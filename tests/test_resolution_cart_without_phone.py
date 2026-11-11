"""E-441: el carrito de Hotmart que llega sin telefono.

El carrito es la captura
``tests/fixtures/hotmart_cart_abandonment_without_phone_att1_20261110.json``
(base de ATT1, evento 19fa20a6 del 2026-11-10, anonimizado): ``data.buyer``
trae solo email y nombre. Los 16 carritos de ATT1 con
``create_recovery_case_failed`` de los 30 dias previos tienen esa forma, y 15
de esos contactos ya tenian su identidad de WhatsApp de la bandeja 11 (la del
formulario) cuando llego el carrito (medido el 2026-11-11 en solo lectura).

Antes, el plan del piloto no tenia identidad y el bridge lo cortaba en Python
(``pilot_plan_cart_recovery_requires_identity``), guardado como
``create_recovery_case_failed``: la persona que habia dejado el formulario con
permiso no recibia ningun mensaje. Las respuestas de PostgREST tienen la forma
que ya usa ``tests/test_resolution.py`` (no son un payload externo).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from bridge.instance_manifest import InstanceManifest
from bridge.resolution import ResolutionError, resolve_event
from bridge.supabase import PilotBoundaryConfig, SupabaseClient

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "hotmart_cart_abandonment_without_phone_att1_20261110.json"
EVENT_ID = "00000000-0000-4000-8000-000000000441"
FORM_IDENTITY = "5215500000441"


def _captured() -> tuple[dict[str, Any], Any]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"]
    config = InstanceManifest.from_toml_file(
        ROOT / "tests" / "fixtures" / "instances" / "att1" / "instancia.toml"
    ).to_commercial_ally_config()
    buyer = payload["data"]["buyer"]
    assert set(buyer) == {"name", "email"}
    # La captura es de la oferta y el producto de ATT1, tal cual.
    assert payload["data"]["product"]["id"] == config.hotmart_product_id
    return payload, config


class _Recorder:
    """PostgREST falso: el contacto por email, sus identidades y lo que se planifica."""

    def __init__(
        self,
        *,
        email_contact: str | None,
        identity_rows: list[dict[str, Any]],
        identity_status: int = 200,
    ) -> None:
        self.email_contact = email_contact
        self.identity_rows = identity_rows
        self.identity_status = identity_status
        self.identity_lookups: list[dict[str, str]] = []
        self.plans: list[dict[str, Any]] = []
        self.statuses: list[dict[str, Any]] = []
        self.created_contacts = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        if request.method == "GET" and path == "/rest/v1/contact_points":
            if params.get("type") == "eq.email" and self.email_contact is not None:
                return httpx.Response(200, json=[{
                    "contact_id": self.email_contact,
                    "normalized_value": params["normalized_value"][3:],
                    "type": "email",
                    "contacts": {
                        "id": self.email_contact,
                        "full_name": "Contacto del formulario",
                        "email": params["normalized_value"][3:],
                        "phone": None,
                        "contact_permission": "unknown",
                        "lifecycle_status": "lead",
                    },
                }], request=request)
            return httpx.Response(200, json=[], request=request)
        if (
            request.method == "GET"
            and path == "/rest/v1/channel_identities"
            and "external_user_id" not in params
            and params.get("channel") == "eq.whatsapp"
        ):
            self.identity_lookups.append(params)
            return httpx.Response(
                self.identity_status,
                json=self.identity_rows if self.identity_status == 200 else {"message": "down"},
                request=request,
            )
        if request.method == "POST" and path == "/rest/v1/contacts":
            self.created_contacts += 1
            return httpx.Response(201, json=[{"id": "contact-new"}], request=request)
        if request.method == "POST" and path == "/rest/v1/contact_points":
            return httpx.Response(201, request=request)
        if request.method == "POST" and path.startswith("/rest/v1/rpc/plan_"):
            self.plans.append({"rpc": path.rsplit("/", 1)[-1], **json.loads(request.content)})
            return httpx.Response(200, json=[{
                "recovery_case_id": "case-1",
                "followup_sequence_id": "sequence-1",
                "scheduled_action_id": "action-1",
                "created": True,
            }], request=request)
        if request.method == "PATCH" and path == "/rest/v1/webhook_events":
            self.statuses.append(json.loads(request.content))
            return httpx.Response(204, request=request)
        if request.method == "GET":
            return httpx.Response(200, json=[], request=request)
        raise AssertionError(f"unexpected request {request.method} {path}")

    def client(self) -> SupabaseClient:
        return SupabaseClient(
            base_url="https://fake.supabase.co",
            service_role_key="fake-service-role-key",
            transport=httpx.MockTransport(self.handler),
        )


def _identity(contact_id: str, external_user_id: str, inbox_id: int) -> dict[str, Any]:
    return {
        "id": f"identity-{external_user_id}",
        "contact_id": contact_id,
        "external_user_id": external_user_id,
        "metadata": {"inbox_id": inbox_id},
    }


def _resolve(recorder: _Recorder, payload: dict[str, Any], config: Any) -> Any:
    return asyncio.run(resolve_event(
        webhook_event_id=EVENT_ID,
        payload=payload,
        supabase=recorder.client(),
        policy_key="att1-recuperacion-un-toque",
        policy_version=1,
        allowed_jid=None,
        chatwoot_account_id=config.chatwoot_account_id,
        chatwoot_inbox_id=config.chatwoot_inbox_id,
        pilot_boundary=PilotBoundaryConfig(
            scope_key="att1-recuperacion",
            scope_version=1,
            tenant_key=config.tenant_ref,
            channel_provider="waba",
            channel_account_ref=f"chatwoot-inbox:{config.chatwoot_inbox_id}",
        ),
        commercial_ally_config=config,
    ))


def test_cart_without_phone_plans_with_the_identity_the_form_saved() -> None:
    payload, config = _captured()
    recorder = _Recorder(
        email_contact="contact-form",
        identity_rows=[_identity("contact-form", FORM_IDENTITY, config.chatwoot_inbox_id)],
    )

    report = _resolve(recorder, payload, config)

    assert report.contact_id == "contact-form"
    assert recorder.created_contacts == 0
    [lookup] = recorder.identity_lookups
    assert lookup["contact_id"] == "eq.contact-form"
    assert lookup["account_id"] == f"eq.chatwoot:{config.chatwoot_account_id}"
    assert lookup["identity_status"] == "eq.active"
    [plan] = recorder.plans
    assert plan["rpc"] == "plan_lancemos_pilot_cart_recovery"
    assert plan["p_contact_id"] == "contact-form"
    assert plan["p_external_user_id"] == FORM_IDENTITY
    assert plan["p_chatwoot_inbox_id"] == config.chatwoot_inbox_id
    assert not any(status.get("processing_status") == "failed" for status in recorder.statuses)


@pytest.mark.parametrize(
    ("email_contact", "rows", "reason"),
    [
        # Contacto nuevo (sin formulario): no hay a quien escribirle.
        (None, [], "pilot_scope_rejected:buyer_without_phone"),
        # Contacto con formulario pero sin identidad de WhatsApp.
        ("contact-form", [], "pilot_scope_rejected:buyer_without_phone"),
        # La identidad es de otra bandeja: no cuenta.
        ("contact-form", [("contact-form", FORM_IDENTITY, "otra")], "pilot_scope_rejected:buyer_without_phone"),
        # Dos identidades activas del contacto en la bandeja: no se elige.
        (
            "contact-form",
            [("contact-form", FORM_IDENTITY, "misma"), ("contact-form", "525500000441", "misma")],
            "pilot_scope_rejected:buyer_without_phone_identity_ambiguous",
        ),
    ],
)
def test_cart_without_phone_and_without_identity_does_not_enter(
    email_contact: str | None,
    rows: list[tuple[str, str, str]],
    reason: str,
) -> None:
    payload, config = _captured()
    recorder = _Recorder(
        email_contact=email_contact,
        identity_rows=[
            _identity(
                contact,
                value,
                config.chatwoot_inbox_id if inbox == "misma" else config.chatwoot_inbox_id + 1,
            )
            for contact, value, inbox in rows
        ],
    )

    with pytest.raises(ResolutionError, match=reason):
        _resolve(recorder, payload, config)

    assert recorder.plans == []
    assert recorder.statuses[-1]["processing_status"] == "failed"
    assert recorder.statuses[-1]["processing_error"] == reason
    if email_contact is None:
        # Sin contacto previo no se busca ninguna identidad.
        assert recorder.identity_lookups == []


def test_cart_without_phone_keeps_the_generic_failure_when_the_read_fails() -> None:
    # Una lectura caida no es un «no entra»: queda la falla de siempre.
    payload, config = _captured()
    recorder = _Recorder(email_contact="contact-form", identity_rows=[], identity_status=503)

    with pytest.raises(ResolutionError, match="create_recovery_case_failed"):
        _resolve(recorder, payload, config)

    assert recorder.plans == []
    assert recorder.statuses[-1]["processing_error"] == "create_recovery_case_failed"
