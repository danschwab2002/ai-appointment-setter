"""Las rutas de automation-expert-read-v1 sobre el manifiesto de ATT1.

La base se simula con la salida real de las tres funciones, capturada por
tests/sql/followup_engine/validate_automation_expert_read.mjs sobre la cadena
completa de migraciones (tests/fixtures/automation_expert/rpc_*.json). Chatwoot
se simula con el fixture capturado de la bandeja 11 de ATT1 el 2026-10-15
(chatwoot_messages_inbox_11_convs_50_17_52_20261015.json). Cada prueba que
cambia algo del fixture lo dice, y nunca inventa la forma: copia mensajes
reales y les cambia valores.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import json
import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge import app as app_module
from bridge.app import Settings, create_app
from bridge.chatwoot import ChatwootHistoryScanLimitError
from bridge.instance_manifest import InstanceManifest
from bridge.supabase import SupabaseError

FIXTURES = Path(__file__).parent / "fixtures"
EXPERT = FIXTURES / "automation_expert"
ATT1 = FIXTURES / "instances" / "att1"
TOKEN = "e" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
LEAD_A = None  # se completa al cargar el fixture


def _rpc(name: str) -> dict[str, object]:
    return json.loads((EXPERT / name).read_text())["result"]


def _chatwoot_fixture() -> dict[str, object]:
    return json.loads((EXPERT / "chatwoot_messages_inbox_11_convs_50_17_52_20261015.json").read_text())


CANARIES = ("CANARY", "5512345678", "example.test", "<tapado>")


def _with_canaries(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    """Prueba 13: un canario en cada campo que el bridge tiene que descartar."""
    result = copy.deepcopy(messages)
    for message in result:
        content = message.get("content")
        marker = " [supportmagician-handoff:x]" if isinstance(content, str) and "supportmagician-handoff" in content else ""
        # Un audio llega sin texto: queda sin texto, y el canario va en sus adjuntos.
        if content is not None:
            message["content"] = "CANARY_CONTENT 5512345678 ana.canary@example.test" + marker
        message["content_attributes"] = {
            "external_error": "131049: This message was not delivered CANARY",
            "in_reply_to_external_id": "CANARY_WAMID",
        }
        message["source_id"] = "CANARY_SOURCE"
        sender = message.get("sender")
        if isinstance(sender, dict):
            for key in list(sender):
                if key not in ("id", "type"):
                    sender[key] = "CANARY_SENDER 5512345678 a@example.test"
        for attachment in message.get("attachments") or []:
            attachment["transcribed_text"] = "CANARY_TRANSCRIPT 5512345678"
            attachment["data_url"] = "https://chatwoot.example.test/CANARY_DATA"
            attachment["thumb_url"] = "https://chatwoot.example.test/CANARY_THUMB"
    return result


class FakeChatwoot:
    def __init__(self, conversations: dict[int, list[dict[str, object]]] | None = None,
                 failure: Exception | None = None, delay: float = 0.0) -> None:
        self.conversations = conversations or {}
        self.failure = failure
        self.delay = delay
        self.calls: list[dict[str, object]] = []

    async def get_conversation_messages(self, *, conversation_id: int, limit: int = 20,
                                        required_message_ids: tuple[int, ...] = (),
                                        require_history_boundary: bool = False) -> list[dict[str, object]]:
        self.calls.append({"conversation_id": conversation_id, "limit": limit,
                           "require_history_boundary": require_history_boundary})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure is not None:
            raise self.failure
        return copy.deepcopy(self.conversations.get(conversation_id, []))


class FakeReader:
    """La base: devuelve la salida real capturada de las tres funciones."""

    def __init__(self, *, history: dict[str, object] | None = None, failure: Exception | None = None,
                 lookup: dict[str, object] | None = None) -> None:
        self.history = history
        self.failure = failure
        self.lookup = lookup
        self.instances: list[dict[str, object]] = []
        self.lookups: list[dict[str, object]] = []

    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return config

    async def read_automation_expert_flows_v1(self, *, instance: dict[str, object]) -> dict[str, object]:
        self.instances.append(instance)
        if self.failure is not None:
            raise self.failure
        return _rpc("rpc_flows_att1.json")

    async def find_automation_expert_lead_v1(self, *, instance: dict[str, object], conversation: int | None,
                                             issuance_ulid: str | None, name: str | None) -> dict[str, object]:
        self.instances.append(instance)
        self.lookups.append({"conversation": conversation, "issuance_ulid": issuance_ulid, "name": name})
        if self.lookup is not None:
            return self.lookup
        if conversation == 501 or name == "Ana":
            return _rpc("rpc_lookup_conversation_501.json")
        if name == "Zoe":
            return _rpc("rpc_lookup_too_many.json")
        return {"result": "not_found", "candidates": []}

    async def read_automation_expert_lead_history_v1(self, *, instance: dict[str, object],
                                                     lead_ref: str) -> dict[str, object] | None:
        self.instances.append(instance)
        if self.failure is not None:
            raise self.failure
        history = self.history if self.history is not None else _rpc("rpc_history_lead_a.json")
        return history if history["lead"]["lead_ref"] == lead_ref else None  # type: ignore[index]


def _manifest() -> InstanceManifest:
    return InstanceManifest.from_toml_file(ATT1 / "instancia.toml")


def _settings(flows: dict[str, bool] | None = None, **overrides: object) -> Settings:
    manifest = _manifest()
    if flows:
        manifest = replace(manifest, flows={**manifest.flows, **flows})
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid=None,
        capture_dir=Path("/tmp/automation-expert-captures"),
        max_age_seconds=300,
        commercial_ally_config=manifest.to_commercial_ally_config(),
        commercial_ally_manifest_path=Path("/instancia/instancia.toml"),
        instance_manifest=manifest,
        hermes_model_name=manifest.agent_model_name,
        agent_bot_id=2,
        chatwoot_base_url="https://chatwoot.example.org",
        agent_provenance_scope_ref="att1-agent-bot",
        pilot_boundary_enabled=False,
        pilot_scope_key="att1-recuperacion",
        pilot_scope_version=5,
        pilot_precheckout_scope_key="att1-primer-contacto",
        pilot_precheckout_scope_version=2,
        automation_expert_read_enabled=True,
        automation_expert_read_token=TOKEN,
    )
    return replace(settings, **overrides)


def _client(reader: FakeReader | None = None, chatwoot: FakeChatwoot | None = None,
            flows: dict[str, bool] | None = None, **overrides: object) -> TestClient:
    return TestClient(create_app(  # type: ignore[arg-type]
        _settings(flows, **overrides),
        supabase_client=reader or FakeReader(),
        chatwoot_client=chatwoot or FakeChatwoot(),
    ))


def _lead_a() -> str:
    return _rpc("rpc_history_lead_a.json")["lead"]["lead_ref"]  # type: ignore[index,return-value]


# --------------------------------------------------------------- el flag y la clave


def test_the_routes_do_not_exist_with_the_flag_off() -> None:
    # Prueba 6: con el flag apagado, 404.
    client = _client(automation_expert_read_enabled=False, automation_expert_read_token=None)
    assert client.get("/internal/automation-expert/v1/flows", headers=AUTH).status_code == 404
    assert client.post("/internal/automation-expert/v1/leads/lookup", headers=AUTH,
                       json={"conversation": 501}).status_code == 404


def test_the_bridge_does_not_start_without_the_manifest() -> None:
    settings = Settings(
        webhook_secret="test-secret", allowed_jid=None,
        capture_dir=Path("/tmp/automation-expert-captures"), max_age_seconds=300,
        automation_expert_read_enabled=True, automation_expert_read_token=TOKEN,
    )
    with pytest.raises(ValueError, match="requires an instance manifest"):
        create_app(settings, supabase_client=FakeReader())  # type: ignore[arg-type]


def test_the_token_has_its_own_rules() -> None:
    with pytest.raises(ValueError, match="at least 32 characters"):
        _client(automation_expert_read_token="corto")
    # Prueba 6: igual a cualquier otro secreto del bridge, no arranca.
    with pytest.raises(ValueError, match="webhook_secret"):
        _client(webhook_secret=TOKEN)
    with pytest.raises(ValueError, match="operator_correlation_read_token"):
        _client(operator_correlation_read_token=TOKEN)


def test_the_flag_is_a_portable_capability_and_ready_reports_it() -> None:
    assert "automation_expert_read_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    with _client() as client:
        response = client.get("/ready")
    assert response.status_code == 200, response.text
    assert response.json()["automation_expert_read"] == "enabled"


@pytest.mark.parametrize("header", [None, "Bearer " + "x" * 40, TOKEN, f"Basic {TOKEN}"])
def test_without_the_token_it_says_nothing(header: str | None) -> None:
    client = _client()
    headers = {} if header is None else {"Authorization": header}
    for method, path in (
        ("GET", "/internal/automation-expert/v1/flows"),
        ("GET", "/internal/automation-expert/v1/flows/carrito"),
        ("POST", "/internal/automation-expert/v1/leads/lookup"),
        ("GET", f"/internal/automation-expert/v1/leads/{_lead_a()}/history"),
    ):
        response = client.request(method, path, headers=headers, json={"conversation": 501})
        assert response.status_code == 401
        assert response.json() == {"detail": "automation_expert_authentication_required"}
        assert response.headers["www-authenticate"] == "Bearer"


# ------------------------------------------------------------------- los flujos


def test_the_flows_come_in_the_schema_format_with_their_provenance() -> None:
    reader = FakeReader()
    client = _client(reader, flows={"carrito": True, "pago_fallido": True, "inbound": True},
                     pilot_boundary_enabled=True, pilot_tenant_key="lancemos",
                     pilot_channel_provider="waba", pilot_channel_account_ref="chatwoot-inbox:11",
                     portable_hotmart_recovery_enabled=True)
    # El pedido no cambia la instancia: la fija el manifiesto.
    response = client.get("/internal/automation-expert/v1/flows?chatwoot_inbox_id=12&tenant_ref=otra",
                          headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contract"] == "automation-expert-read-v1"
    assert body["contract_version"].startswith("1.")
    assert body["instance"]["chatwoot_inbox_id"] == "11"
    assert reader.instances[0]["chatwoot_inbox_id"] == 11
    assert reader.instances[0]["hotmart_product_id"] == "5071808"
    flows = {flow["flow_key"]: flow for flow in body["data"]["flows"]}
    assert list(flows) == ["precheckout", "carrito", "pago_fallido", "reactivacion", "descuento"]
    cart = flows["carrito"]
    assert cart["version"] == {"scope": "att1-recuperacion v5", "policy": "att1-recuperacion-un-toque v1"}
    assert cart["status"]["state"] == "on" and cart["status"]["detail"] == "armed"
    assert cart["usage"]["daily_cap"] == 80 and cart["usage"]["pool"] == "att1-recuperacion"
    assert cart["definition"]["esquema"] == "setter-flujo/v1"
    assert cart["definition"]["version"] == 1 and cart["definition"]["estado"] == "publicada"
    assert cart["definition"]["horario"] == {"zona": "America/Mexico_City", "desde": "09:00", "hasta": "21:00"}
    assert cart["definition"]["termina"] == {"si": ["compra"], "vence": "1d"}
    assert cart["priority"]["no_arranca_mientras"] == ["pago_fallido"]
    assert cart["provenance"]["/priority/le_gana_a"]["from"] == "code"
    assert cart["observed"]["accepted_by_chatwoot"] == 1
    # El pago fallido no tiene su flag en este runtime: apagado, no le gana a nadie.
    assert flows["pago_fallido"]["status"]["state"] == "off"
    assert flows["precheckout"]["priority"]["le_ganan"] == ["carrito"]
    assert flows["precheckout"]["definition"]["pasos"][0] == {"esperar": "60m"}
    # El primer contacto esta apagado en el manifiesto: off, aunque su scope este armado.
    assert flows["precheckout"]["status"] == {"state": "off", "detail": "flag_off", "since": None}
    assert flows["precheckout"]["version"] == {
        "scope": "att1-primer-contacto v2", "policy": "att1-primer-contacto-formulario v1"}
    assert flows["reactivacion"]["status"]["state"] == "off"
    assert flows["descuento"]["status"]["state"] == "off"
    assert flows["descuento"]["version"] is None
    assert body["data"]["in_code"][0] == {
        "key": "inbound", "name": "El agente que contesta", "status": "on",
        "provenance": {"from": "manifest", "ref": "[flujos].inbound"}}
    assert {gap["code"] for gap in body["gaps"]} >= {"env_value_unversioned"}
    assert body["data"]["events_without_flow"][0]["event_type"] == "PURCHASE_BILLET_PRINTED"
    assert body["data"]["shared"]["gate"] is None
    assert body["data"]["shared"]["per_person_cap"]["value"] == "sin_tope"


def test_one_flow_unknown_flow_and_invalid_key() -> None:
    client = _client()
    one = client.get("/internal/automation-expert/v1/flows/carrito", headers=AUTH)
    assert one.status_code == 200
    assert [flow["flow_key"] for flow in one.json()["data"]["flows"]] == ["carrito"]
    assert client.get("/internal/automation-expert/v1/flows/ficha", headers=AUTH).json() == {
        "detail": "flow_not_found"}
    bad = client.get("/internal/automation-expert/v1/flows/Bad-Key", headers=AUTH)
    assert bad.status_code == 422 and bad.json() == {"detail": "invalid_flow_key"}


def test_a_database_failure_is_a_closed_503() -> None:
    client = _client(FakeReader(failure=SupabaseError("automation_expert_flows_failed: HTTP 500")))
    response = client.get("/internal/automation-expert/v1/flows", headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"detail": "automation_expert_read_unavailable"}


# ------------------------------------------------------------------- la busqueda


def test_the_lookup_finds_a_lead_by_conversation_and_names_it() -> None:
    reader = FakeReader()
    response = _client(reader).post("/internal/automation-expert/v1/leads/lookup", headers=AUTH,
                                    json={"conversation": 501})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["result"] == "found"
    assert data["lead_ref"] == _lead_a()
    assert data["candidates"][0]["label"] == "Ana · conversación #501"
    assert data["candidates"][0]["conversations"] == [
        {"number": 501, "url": "https://chatwoot.example.org/app/accounts/2/conversations/501"}]
    assert reader.lookups == [{"conversation": 501, "issuance_ulid": None, "name": None}]


@pytest.mark.parametrize("body", [
    {"name": "+52 1 55 1234 5678"}, {"name": "(55) 1234-5678"}, {"name": "ana.canary@example.test"},
    {"conversation": 5512345678}, {"conversation": "55 1234 5678"},
])
def test_a_phone_or_a_mail_is_rejected_before_the_database(body: dict[str, object]) -> None:
    reader = FakeReader()
    response = _client(reader).post("/internal/automation-expert/v1/leads/lookup", headers=AUTH, json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "identifier_not_allowed"}
    assert reader.lookups == []


def test_lookup_errors_and_too_many_candidates(caplog: pytest.LogCaptureFixture) -> None:
    client = _client()
    two_keys = client.post("/internal/automation-expert/v1/leads/lookup", headers=AUTH,
                           json={"conversation": 501, "name": "Ana"})
    assert two_keys.status_code == 422 and two_keys.json() == {"detail": "invalid_lookup"}
    not_json = client.post("/internal/automation-expert/v1/leads/lookup", headers=AUTH, content=b"nope")
    assert not_json.status_code == 422
    with caplog.at_level(logging.INFO):
        missing = client.post("/internal/automation-expert/v1/leads/lookup", headers=AUTH,
                              json={"name": "Nadie Conocido"})
    assert missing.status_code == 404 and missing.json() == {"detail": "lead_not_found"}
    # El log de acceso no lleva lo que escribio la persona.
    assert "Nadie" not in caplog.text
    many = client.post("/internal/automation-expert/v1/leads/lookup", headers=AUTH, json={"name": "Zoe"})
    assert many.status_code == 200
    assert many.json()["data"] == {"result": "too_many_candidates", "lead_ref": None, "candidates": []}


# ------------------------------------------------------------------- la historia


def _history_with_conversations(numbers: list[int], *, templates: dict[int, int],
                                adopted: list[dict[str, object]] | None = None) -> dict[str, object]:
    """La historia real del lead A, con sus conversaciones cambiadas por las del
    fixture de Chatwoot y los ids de sus plantillas por los de esas
    conversaciones."""
    history = copy.deepcopy(_rpc("rpc_history_lead_a.json"))
    history["lead"]["conversations"] = numbers  # type: ignore[index]
    for moment in history["moments"]:  # type: ignore[union-attr]
        message_id = moment["extra"].get("chatwoot_message_id")
        if message_id in templates:
            moment["extra"]["chatwoot_message_id"] = templates[message_id]
    if adopted is not None:
        history["adopted_conversations"] = adopted
    return history


def _chatwoot_conversations(**changes: object) -> dict[int, list[dict[str, object]]]:
    fixture = _chatwoot_fixture()["conversations"]
    return {int(number): _with_canaries(conversation["payload"]) for number, conversation in fixture.items()}  # type: ignore[union-attr]


def test_the_history_joins_the_database_and_chatwoot_without_private_data() -> None:
    conversations = _chatwoot_conversations()
    # Una nota privada del equipo: la nota de la derivacion real de la 50, sin
    # la marca (la bandeja 11 no tiene ninguna al 2026-10-15).
    team_note = copy.deepcopy(next(m for m in conversations[50] if m["id"] == 2839))
    team_note.update({"id": 3330, "content": "CANARY_TEAM_NOTE", "created_at": team_note["created_at"] + 60})
    conversations[50].append(team_note)
    history = _history_with_conversations(
        [17, 50, 52], templates={3001: 2820, 3101: 2831},
        adopted=[{"conversation": 50, "coupon_sent": False, "last_agent_turn_at": None}])
    chatwoot = FakeChatwoot(conversations)
    response = _client(FakeReader(history=history), chatwoot).get(
        f"/internal/automation-expert/v1/leads/{_lead_a()}/history", headers=AUTH)
    assert response.status_code == 200, response.text
    serialized = response.text
    for canary in CANARIES:
        assert canary not in serialized, canary
    body = response.json()
    past = body["data"]["past"]
    deliveries = {m["evidence"]["id"]: m for m in past if m["kind"] == "meta_delivery"}
    assert deliveries["2820"]["delivery_status"] == "read"
    assert deliveries["2820"]["flow_key"] == "carrito"
    failed = deliveries["2831"]
    assert failed["delivery_status"] == "failed"
    assert failed["meta_error_code"] == "131049"
    assert "marketing" in failed["explanation"]
    activity = {m["evidence"]["id"]: m for m in past if m["kind"] == "conversation_activity"}
    # La actividad de Chatwoot (message_type 2) no sale.
    assert not {"2837", "2838", "2771", "2772", "3054"} & set(activity)
    assert activity["2839"]["actor_type"] == "system" and activity["2839"]["private"] is True
    assert "delivery_status" not in activity["2839"]
    assert activity["3330"]["actor_type"] == "human_agent" and activity["3330"]["private"] is True
    assert activity["3037"]["actor_type"] == "human_agent" and activity["3037"]["delivery_status"] == "read"
    assert activity["3329"]["delivery_status"] == "failed"
    # El audio de la persona cuenta como su mensaje, aunque no traiga texto.
    assert activity["2770"]["actor_type"] == "prospect"
    assert activity["2769"]["actor_type"] == "ai_agent"
    # El cupon esta apagado en la instancia: no hay momento del cupon.
    assert not [m for m in past if m["kind"] == "coupon_not_sent"]
    # Sin huecos de mas: cada codigo de la historia de A esta en el catalogo.
    assert {gap["code"] for gap in body["gaps"]} == {"payment_slip_not_attached", "agent_release_not_persisted"}
    assert body["data"]["lead"]["label"] == "Ana · conversación #52"
    assert body["data"]["now"]["purchased"] is True
    for call in chatwoot.calls:
        assert call["limit"] == 200 and call["require_history_boundary"] is True
    stopped = next(m for m in past if m["kind"] == "stopped")
    assert stopped["reason"] == "le_gano:carrito" and stopped["decision"] == "cancel"
    assert stopped["explanation"]


def test_a_conversation_with_messages_of_another_inbox_stays_out() -> None:
    conversations = _chatwoot_conversations()
    conversations[50][3]["inbox_id"] = 12
    history = _history_with_conversations([50], templates={3001: 2820})
    response = _client(FakeReader(history=history), FakeChatwoot(conversations)).get(
        f"/internal/automation-expert/v1/leads/{_lead_a()}/history", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert not [m for m in body["data"]["past"] if m["kind"] in ("meta_delivery", "conversation_activity")]
    gap = next(g for g in body["gaps"] if g["code"] == "chatwoot_not_read")
    assert gap["what"] == "la conversación #50" and "otra bandeja" in gap["why"]


@pytest.mark.parametrize("chatwoot", [
    FakeChatwoot(failure=httpx.ConnectError("caido")),
    FakeChatwoot(failure=ChatwootHistoryScanLimitError("history_boundary_beyond_scan_limit")),
    FakeChatwoot(conversations={50: []}, delay=0.5),
])
def test_without_chatwoot_the_history_still_comes_with_its_gap(
    chatwoot: FakeChatwoot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Prueba 14: caido, con error, mas lento que el tope o con demasiadas paginas.
    monkeypatch.setattr(app_module, "CHATWOOT_READ_TIMEOUT_SECONDS", 0.1)
    history = _history_with_conversations([50], templates={3001: 2820})
    response = _client(FakeReader(history=history), chatwoot).get(
        f"/internal/automation-expert/v1/leads/{_lead_a()}/history", headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert any(gap["code"] == "chatwoot_not_read" for gap in body["gaps"])
    kinds = {m["kind"] for m in body["data"]["past"]}
    assert "accepted_by_chatwoot" in kinds and "meta_delivery" not in kinds


def test_the_history_pages_with_an_opaque_cursor() -> None:
    client = _client()
    url = f"/internal/automation-expert/v1/leads/{_lead_a()}/history"
    first = client.get(f"{url}?limit=5", headers=AUTH).json()
    assert len(first["data"]["past"]) == 5
    cursor = first["page"]["next_cursor"]
    assert cursor
    second = client.get(f"{url}?limit=100&cursor={cursor}", headers=AUTH).json()
    first_ids = [m["evidence"]["id"] + m["kind"] for m in first["data"]["past"]]
    second_ids = [m["evidence"]["id"] + m["kind"] for m in second["data"]["past"]]
    assert not set(first_ids) & set(second_ids)
    assert second["page"]["next_cursor"] is None
    whole = client.get(url, headers=AUTH).json()
    assert len(whole["data"]["past"]) == len(first_ids) + len(second_ids)


@pytest.mark.parametrize("path,detail,code", [
    ("not-a-uuid/history", "invalid_lead_ref", 422),
    ("00000000-0000-0000-0000-000000000000/history", "lead_not_found", 404),
    ("{lead}/history?limit=0", "limit_out_of_range", 422),
    ("{lead}/history?cursor=garbage!", "invalid_cursor", 422),
])
def test_history_errors(path: str, detail: str, code: int) -> None:
    url = "/internal/automation-expert/v1/leads/" + path.replace("{lead}", _lead_a())
    response = _client().get(url, headers=AUTH)
    assert response.status_code == code
    assert response.json() == {"detail": detail}


def test_lead_c_attached_by_identity_carries_its_reasons_and_gaps() -> None:
    history = _rpc("rpc_history_lead_c.json")
    lead_ref = history["lead"]["lead_ref"]  # type: ignore[index]
    response = _client(FakeReader(history=history)).get(
        f"/internal/automation-expert/v1/leads/{lead_ref}/history", headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    rejected = {m["control_state"]: m for m in body["data"]["past"] if m["kind"] == "rejected_on_entry"}
    # Pausado es «flujo pausado»; recien activada la version (inactive), no.
    assert rejected["paused"]["reason"] == "flujo_pausado"
    assert rejected["inactive"]["reason"] is None
    assert rejected["paused"]["certainty"] == "inferred" and rejected["paused"]["attached_by"] == "identity"
    codes = {gap["code"] for gap in body["gaps"]}
    assert {"unattached_hotmart_event", "purchase_channel_not_persisted", "unknown_reason_code"} <= codes
    assert body["data"]["lead"]["identity"]["opted_out"] is True


def test_a_failed_template_without_the_meta_code_leaves_its_gap() -> None:
    conversations = {52: copy.deepcopy(_chatwoot_fixture()["conversations"]["52"]["payload"])}  # type: ignore[index]
    for message in conversations[52]:
        message["content_attributes"] = {}
    history = _history_with_conversations([52], templates={3101: 2831})
    response = _client(FakeReader(history=history), FakeChatwoot(conversations)).get(
        f"/internal/automation-expert/v1/leads/{_lead_a()}/history", headers=AUTH)
    failed = next(m for m in response.json()["data"]["past"] if m["kind"] == "meta_delivery")
    assert failed["delivery_status"] == "failed" and "meta_error_code" not in failed
    assert "meta_failure_reason_not_persisted" in {gap["code"] for gap in response.json()["gaps"]}


def test_lead_b_shows_the_lease_loop_once_and_what_comes_next() -> None:
    history = _rpc("rpc_history_lead_b.json")
    lead_ref = history["lead"]["lead_ref"]  # type: ignore[index]
    response = _client(FakeReader(history=history)).get(
        f"/internal/automation-expert/v1/leads/{lead_ref}/history", headers=AUTH)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    rejected = [m for m in data["past"] if m["kind"] == "start_rejected"]
    assert len(rejected) == 1 and rejected[0]["count"] == 3
    assert rejected[0]["reason"] is None and rejected[0]["reason_code"] == "pilot_scope_version_mismatch"
    assert rejected[0]["explanation"]
    # El caso B del contrato: el carrito llego despues del formulario y le gana.
    expected = data["upcoming"][0]["expected"]
    assert data["upcoming"][0]["flow_key"] == "precheckout"
    assert expected["decision"] == "cancel" and expected["reason"] == "le_gano:carrito"
    assert expected["certainty"] == "inferred"


def test_the_lookup_body_has_a_cap() -> None:
    reader = FakeReader()
    response = _client(reader).post("/internal/automation-expert/v1/leads/lookup", headers=AUTH,
                                    content=json.dumps({"name": "a" * 5000}).encode())
    assert response.status_code == 422 and response.json() == {"detail": "invalid_lookup"}
    assert reader.lookups == []


def test_conversations_beyond_five_leave_their_gap() -> None:
    history = _history_with_conversations([11, 12, 13, 14, 15, 16, 17], templates={})
    chatwoot = FakeChatwoot({})
    response = _client(FakeReader(history=history), chatwoot).get(
        f"/internal/automation-expert/v1/leads/{_lead_a()}/history", headers=AUTH)
    assert response.status_code == 200
    assert sorted(call["conversation_id"] for call in chatwoot.calls) == [13, 14, 15, 16, 17]
    skipped = [gap["what"] for gap in response.json()["gaps"] if gap["code"] == "chatwoot_not_read"]
    assert skipped == ["la conversación #11", "la conversación #12"]


def test_an_unexpected_failure_is_a_503_with_only_its_type_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reader = FakeReader(failure=ValueError("invalid literal 5512345678"))
    with caplog.at_level(logging.WARNING):
        response = _client(reader).get("/internal/automation-expert/v1/flows", headers=AUTH)
    assert response.status_code == 503
    assert "5512345678" not in caplog.text
    assert "error_type=ValueError" in caplog.text


def test_the_postgrest_client_reads_by_get_and_searches_by_post() -> None:
    from bridge.supabase import AutomationExpertResponseTooLarge, SupabaseClient

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("read_automation_expert_lead_history_v1"):
            return httpx.Response(400, json={"code": "54000", "message": "automation_expert_response_too_large"})
        if request.url.path.endswith("find_automation_expert_lead_v1"):
            return httpx.Response(200, json={"result": "not_found", "candidates": []})
        return httpx.Response(200, json={"scopes": []})

    client = SupabaseClient(base_url="https://db.example.test", service_role_key="secret",
                            transport=httpx.MockTransport(handler))
    instance = {"tenant_ref": "lancemos", "chatwoot_inbox_id": 11}
    assert asyncio.run(client.read_automation_expert_flows_v1(instance=instance)) == {"scopes": []}
    assert seen[0].method == "GET"
    assert json.loads(seen[0].url.params["p_instance"]) == instance
    found = asyncio.run(client.find_automation_expert_lead_v1(
        instance=instance, conversation=None, issuance_ulid=None, name="Ana"))
    assert found["result"] == "not_found"
    assert seen[1].method == "POST" and "Ana" not in str(seen[1].url)
    assert json.loads(seen[1].content)["p_name"] == "Ana"
    with pytest.raises(AutomationExpertResponseTooLarge):
        asyncio.run(client.read_automation_expert_lead_history_v1(
            instance=instance, lead_ref="11111111-1111-1111-1111-111111111111"))
    assert seen[2].method == "GET"


def test_a_third_request_at_once_is_busy() -> None:
    gate = asyncio.Event()

    class SlowReader(FakeReader):
        async def read_automation_expert_flows_v1(self, *, instance: dict[str, object]) -> dict[str, object]:
            await gate.wait()
            return await super().read_automation_expert_flows_v1(instance=instance)

    app = create_app(_settings(), supabase_client=SlowReader(), chatwoot_client=FakeChatwoot())  # type: ignore[arg-type]

    async def scenario() -> list[int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as client:
            first = asyncio.create_task(client.get("/internal/automation-expert/v1/flows", headers=AUTH))
            second = asyncio.create_task(client.get("/internal/automation-expert/v1/flows", headers=AUTH))
            await asyncio.sleep(0.05)
            third = await client.get("/internal/automation-expert/v1/flows", headers=AUTH)
            gate.set()
            return [third.status_code, (await first).status_code, (await second).status_code, third.json()["detail"]]

    third, first, second, detail = asyncio.run(scenario())
    assert (third, detail) == (503, "automation_expert_busy")
    assert (first, second) == (200, 200)


def test_too_large_is_its_own_503() -> None:
    from bridge.supabase import AutomationExpertResponseTooLarge

    reader = FakeReader(failure=AutomationExpertResponseTooLarge("automation_expert_history_too_large"))
    response = _client(reader).get(f"/internal/automation-expert/v1/leads/{_lead_a()}/history", headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"detail": "automation_expert_response_too_large"}
    history = copy.deepcopy(_rpc("rpc_history_lead_a.json"))
    history["upcoming"] = [{
        "flow_key": "carrito", "flow_version": None, "step_key": "first_contact",
        "due_at": "2026-10-15T12:00:00+00:00", "expires_at": None,
        "expected": {"decision": "execute", "reason_code": "eligible_for_execution"},
    }] * 21
    response = _client(FakeReader(history=history)).get(
        f"/internal/automation-expert/v1/leads/{_lead_a()}/history", headers=AUTH)
    assert response.json() == {"detail": "automation_expert_response_too_large"}


def test_a_real_chatwoot_client_with_too_many_pages_leaves_the_gap() -> None:
    from bridge.chatwoot import ChatwootClient

    template = copy.deepcopy(_chatwoot_fixture()["conversations"]["50"]["payload"][1])  # type: ignore[index]
    pages = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal pages
        pages += 1
        before = int(request.url.params.get("before", "100000"))
        payload = []
        for offset in range(1, 21):
            message = copy.deepcopy(template)
            message["id"] = before - offset
            payload.append(message)
        return httpx.Response(200, json={"payload": payload})

    chatwoot = ChatwootClient(base_url="https://chatwoot.example.org", account_id=2, access_token="t",
                              transport=httpx.MockTransport(handler))
    history = _history_with_conversations([50], templates={})
    response = _client(FakeReader(history=history), chatwoot).get(  # type: ignore[arg-type]
        f"/internal/automation-expert/v1/leads/{_lead_a()}/history", headers=AUTH)
    assert response.status_code == 200
    gap = next(g for g in response.json()["gaps"] if g["code"] == "chatwoot_not_read")
    assert "tope de páginas" in gap["why"]
    assert pages == 10


def test_lead_f_carries_the_branches_of_the_start_and_the_team_hold() -> None:
    history = _rpc("rpc_history_lead_f.json")
    lead_ref = history["lead"]["lead_ref"]  # type: ignore[index]
    response = _client(FakeReader(history=history)).get(
        f"/internal/automation-expert/v1/leads/{lead_ref}/history?limit=100", headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    reasons = {m["reason_code"]: m["reason"] for m in body["data"]["past"] if m["kind"] == "start_rejected"}
    assert reasons == {
        "pilot_runtime_not_armed": "flujo_pausado",
        "pilot_total_budget_exhausted": "tope_del_flujo",
        "pilot_daily_budget_exhausted": "tope_del_flujo",
        None: None,
    }
    assert "start_rejection_not_persisted" in {gap["code"] for gap in body["gaps"]}
    held = next(item for item in body["data"]["upcoming"] if item["flow_key"] == "carrito")
    assert held["expected"]["decision"] == "pause" and held["expected"]["reason"] == "tomada_por_el_equipo"
    uncertain = next(m for m in body["data"]["past"] if m["kind"] == "delivery_uncertain_or_failed")
    assert uncertain["reason"] == "envio_incierto"
