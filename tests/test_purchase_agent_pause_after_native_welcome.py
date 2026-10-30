"""E-190: la pausa al comprar, despues de la bienvenida nativa (Maribel #27).

Desde E-165 el worker no pausa al comprador que tomo un flujo nativo de la
compra: la bienvenida va a la misma conversacion, y una conversacion pausada
frena el envio. Con el flujo en ``cohorte`` eso toca a pocos; con el flujo
``prendido`` lo toma todo comprador, y en Maribel (``PURCHASE_AGENT_PAUSE_ENABLED``,
la replica de «Ventas maribel» de GHL) ningun comprador quedaba pausado: el
agente le seguia contestando como a un prospecto.

Con ``PURCHASE_AGENT_PAUSE_AFTER_NATIVE_WELCOME_ENABLED`` (apagado por defecto),
el despachador pausa la conversacion apenas Chatwoot acepta el envio de un paso
nativo con ``permiso: compra``: la misma etiqueta, el mismo team y la misma nota
que al comprar, y la reanudacion por silencio la respeta. Un flujo nativo tiene
un solo envio por camino y nada despues (flow-engine-v2), asi que la pausa no
frena ningun paso del flujo.

Datos reales:

* la bienvenida de Maribel, ``maribel_bienvenida_01``, del catalogo del inbox 13
  (``chatwoot_message_templates_inbox_13_bienvenida_20261030.json``, E-190);
* la compra aprobada mas reciente de Maribel con la oferta de la pauta
  (``hotmart_purchase_approved_maribel_20261029.json``, anonimizada en el
  servidor antes de salir);
* el manifiesto de Maribel (``tests/fixtures/instances/maribel/instancia.toml``):
  inbox 13, team 3, zona America/Mexico_City.

El despacho es el de ``test_flow_engine_native_dispatch`` (el doble de la base
y de Chatwoot del inbox 11): la pausa nueva se engancha despues de su envio
aceptado.
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_flow_engine_bridge_settings import engine_settings
from test_flow_engine_native_dispatch import (
    LABEL_MACRO_ID,
    _dispatcher,
    _NativeAuthority,
    _NativeChatwoot,
    _run,
    _step,
    _templates,
)
from test_att1_production_settings import _start
from test_cart_discount_first_touch import _app

from bridge import app as app_module
from bridge.app import Settings
from bridge.approved_templates import is_static_url, parse_approved_template
from bridge.hotmart import parse_hotmart_purchase_payload
from bridge.native_flows import NATIVE_PURCHASE_PERMISSION, native_step_categories
from bridge.supabase import PurchaseAgentPauseLookup, SupabaseError
from bridge.chatwoot import ChatwootAssignmentConflictError, ChatwootProtocolError
from bridge.purchase_agent_pause import (
    PURCHASE_PAUSE_PENDING_NOTE_BODY,
    PURCHASE_PAUSE_PENDING_NOTE_MARKER_PREFIX,
    PurchaseAgentPause,
    purchase_pause_note_body,
    purchase_pause_note_marker,
    purchase_pause_note_present,
)

FIXTURES = Path(__file__).parent / "fixtures"
TEMPLATES = json.loads(
    (FIXTURES / "chatwoot_message_templates_inbox_13_bienvenida_20261030.json").read_text(encoding="utf-8"))
[WELCOME] = TEMPLATES["message_templates"]
PURCHASE = json.loads(
    (FIXTURES / "hotmart_purchase_approved_maribel_20261029.json").read_text(encoding="utf-8"))
MANIFEST = tomllib.loads(
    (FIXTURES / "instances" / "maribel" / "instancia.toml").read_text(encoding="utf-8"))
ACTION_ID = "5c1d2e3f-4a5b-4c6d-8e7f-90a1b2c3d4e5"


class _PauseChatwoot:
    """Lo que la pausa le pide a Chatwoot: la etiqueta y el team, despues la nota."""

    def __init__(self, *, fail: bool = False, note_error: bool = False) -> None:
        self.fail = fail
        self.note_error = note_error
        self.paused: list[dict[str, object]] = []
        self.notes: list[dict[str, object]] = []

    async def pause_purchased_conversation(self, **kwargs: object) -> str:
        if self.fail:
            raise RuntimeError("caido")
        self.paused.append(kwargs)
        return "team_assigned"

    async def ensure_marked_private_note(self, **kwargs: object) -> bool:
        self.notes.append(kwargs)
        if self.note_error:
            raise RuntimeError("nota caida")
        return True


class _NoReads:
    """La pausa despues de la bienvenida no busca al comprador en la base."""

    async def read_purchase_agent_pause_conversations(self, **_: object) -> Any:
        raise AssertionError("la conversacion es la de la bienvenida: no se busca")


def _maribel_pause(chatwoot: _PauseChatwoot, *, inbox_id: int | None = None) -> PurchaseAgentPause:
    return PurchaseAgentPause(
        supabase=_NoReads(),  # type: ignore[arg-type]
        chatwoot=chatwoot,  # type: ignore[arg-type]
        chatwoot_account_id=MANIFEST["chatwoot"]["account_id"],
        chatwoot_inbox_id=inbox_id if inbox_id is not None else MANIFEST["chatwoot"]["inbox_id"],
        inbound_scope_key=MANIFEST["inbound"]["scope_key"],
        team_id=MANIFEST["chatwoot"]["equipo_derivacion"],
        time_zone=MANIFEST["instancia"]["zona_horaria"],
    )


def _purchased_at() -> str:
    approved_ms = PURCHASE["payload"]["data"]["purchase"]["approved_date"]
    return datetime.fromtimestamp(approved_ms / 1000, tz=UTC).isoformat()


# -- El dato real: la bienvenida de Maribel la manda el motor nativo ------------


def test_the_fixture_is_the_captured_maribel_welcome() -> None:
    assert WELCOME["name"] == "maribel_bienvenida_01"
    assert (WELCOME["status"], WELCOME["category"], WELCOME["language"]) == ("APPROVED", "UTILITY", "es_MX")
    body = next(c["text"] for c in WELCOME["components"] if c["type"] == "BODY")
    assert body.count("{{") == 1 and "{{1}}" in body
    [buttons] = [c["buttons"] for c in WELCOME["components"] if c["type"] == "BUTTONS"]
    assert [(b["type"], is_static_url(b["url"])) for b in buttons] == [("URL", True)]
    assert {"type": "HEADER", "format": "TEXT", "text": "Notificación"} in WELCOME["components"]


def test_the_motor_builds_the_maribel_welcome_with_permiso_compra() -> None:
    # La categoria es la que exige el permiso compra (solo utilidad) y el producto es el del manifiesto.
    approved = parse_approved_template(
        {"message_templates": [WELCOME]},
        template_name="maribel_bienvenida_01",
        expected_language="es_MX",
        expected_category=native_step_categories(NATIVE_PURCHASE_PERMISSION),
        parameter_count=1,
        parameterless_buttons_only=True,
    )
    product = MANIFEST["hotmart"]["product_name"]
    assert approved.processed_params({"1": product}) == {"body": {"1": product}}
    assert approved.button_url is None


def test_the_captured_purchase_is_the_maribel_product_and_offer() -> None:
    payload = PURCHASE["payload"]
    assert payload["event"] == "PURCHASE_APPROVED"
    assert payload["data"]["product"]["id"] == MANIFEST["hotmart"]["product_id"]
    assert payload["data"]["purchase"]["offer"]["code"] == MANIFEST["hotmart"]["ofertas"][0]["codigo"]
    # Anonimizada: el comprador entero es marcador.
    assert all(
        value.startswith("<")
        for key, value in payload["data"]["buyer"].items()
        if key != "address"
    )


# -- La pausa, despues de la bienvenida -----------------------------------------


def test_after_the_welcome_the_conversation_is_paused_with_the_purchase_note() -> None:
    chatwoot = _PauseChatwoot()
    outcome = asyncio.run(_maribel_pause(chatwoot).pause_after_native_welcome(
        action_id=ACTION_ID, conversation_id=200, purchased_at=_purchased_at(),
    ))
    assert outcome == "paused"
    assert chatwoot.paused == [{"conversation_id": 200, "expected_inbox_id": 13, "team_id": 3}]
    [note] = chatwoot.notes
    approved = datetime.fromisoformat(_purchased_at())
    assert note["note_body"] == purchase_pause_note_body(approved, time_zone="America/Mexico_City")
    assert note["idempotency_marker"] == purchase_pause_note_marker(ACTION_ID)
    # La reanudacion por silencio del equipo reconoce la nota: no la devuelve al agente.
    content = f"{note['note_body']}\n\n{note['idempotency_marker']}"
    assert purchase_pause_note_present([{"private": True, "content": content}])


def test_a_failed_pause_or_note_never_raises() -> None:
    assert asyncio.run(_maribel_pause(_PauseChatwoot(fail=True)).pause_after_native_welcome(
        action_id=ACTION_ID, conversation_id=200, purchased_at=_purchased_at(),
    )) == "failed"
    chatwoot = _PauseChatwoot(note_error=True)
    assert asyncio.run(_maribel_pause(chatwoot).pause_after_native_welcome(
        action_id=ACTION_ID, conversation_id=200, purchased_at=None,
    )) == "paused"
    assert len(chatwoot.paused) == 1


# -- En el despachador ----------------------------------------------------------


def _native(entry_permission: str | None) -> _NativeAuthority:
    authority = _NativeAuthority(step=_step(entry_permission=entry_permission))
    authority.action = replace(authority.action, action_id=ACTION_ID)
    authority.attempt = replace(authority.attempt, action_id=ACTION_ID)
    authority.context = replace(authority.context, action_id=ACTION_ID)
    return authority


def _dispatch(tmp_path: Path, authority: _NativeAuthority, pause: PurchaseAgentPause | None) -> _NativeChatwoot:
    chatwoot = _NativeChatwoot()
    dispatcher = _dispatcher(authority, chatwoot, tmp_path)
    # El dispatcher del harness no recibe el kwarg nuevo: el cableado lo prueba
    # test_the_bridge_hands_the_pause_to_the_dispatcher_only_with_the_flag.
    dispatcher._purchase_pause_after_native_welcome = pause
    _run(dispatcher)
    return chatwoot


def test_the_dispatcher_pauses_the_buyer_once_the_purchase_welcome_is_accepted(tmp_path: Path) -> None:
    pause_client = _PauseChatwoot()
    authority = _native(NATIVE_PURCHASE_PERMISSION)
    chatwoot = _dispatch(tmp_path, authority, _maribel_pause(pause_client, inbox_id=11))

    assert len(_templates(chatwoot)) == 1 and authority.acceptances
    # Despues de la etiqueta cliente y su nota (la cola del paso), la pausa.
    assert chatwoot.labels == ["cliente"]
    assert [call["conversation_id"] for call in pause_client.paused] == [200]
    [note] = pause_client.notes
    assert note["idempotency_marker"] == purchase_pause_note_marker(ACTION_ID)


@pytest.mark.parametrize("entry_permission", ["formulario", None])
def test_a_cart_flow_or_a_base_without_the_permission_is_not_paused(
    tmp_path: Path, entry_permission: str | None
) -> None:
    pause_client = _PauseChatwoot()
    authority = _native(entry_permission)
    _dispatch(tmp_path, authority, _maribel_pause(pause_client, inbox_id=11))
    assert pause_client.paused == [] and pause_client.notes == []


def test_without_the_flag_the_welcome_goes_as_today(tmp_path: Path) -> None:
    # ATT1 y Johanna: sin el flag, el despachador no recibe la pausa y nada cambia.
    authority = _native(NATIVE_PURCHASE_PERMISSION)
    chatwoot = _dispatch(tmp_path, authority, None)
    assert len(_templates(chatwoot)) == 1
    assert chatwoot.labels == ["cliente"]
    # Ni la etiqueta de pausa, ni la asignacion al team, ni otro macro que el de cliente.
    posts = [path for method, path, _ in chatwoot.requests if method == "POST"]
    assert any(path.endswith(f"/macros/{LABEL_MACRO_ID}/execute") for path in posts)
    assert not [
        path for path in posts
        if path.endswith(("/labels", "/assignments"))
        or ("/macros/" in path and not path.endswith(f"/macros/{LABEL_MACRO_ID}/execute"))
    ]


class _FoundBuyer:
    """La base de la pausa: encuentra las conversaciones del comprador (como al comprar)."""

    def __init__(self, *conversation_ids: int) -> None:
        self.conversation_ids = conversation_ids
        self.reads: list[dict[str, object]] = []

    async def read_purchase_agent_pause_conversations(self, **kwargs: object) -> Any:
        self.reads.append(kwargs)
        return PurchaseAgentPauseLookup("found", self.conversation_ids)


def _pause_reading(chatwoot: _PauseChatwoot, base: Any) -> PurchaseAgentPause:
    return replace(_maribel_pause(chatwoot, inbox_id=11), supabase=base)


class _LastRetry(_NativeAuthority):
    """El ultimo intento: la base cierra la accion aunque el bridge pida otro (sin reintentos)."""

    async def finalize_followup_delivery_attempt(self, **kwargs: object) -> object:
        finalized = await super().finalize_followup_delivery_attempt(**kwargs)
        if kwargs["outcome"] == "failed_before_request":
            return type("Finalized", (), {"status": "permanent_failed"})()
        return finalized


def _last_retry(entry_permission: str) -> _LastRetry:
    authority = _LastRetry(step=_step(entry_permission=entry_permission))
    authority.action = replace(authority.action, action_id=ACTION_ID)
    authority.attempt = replace(authority.attempt, action_id=ACTION_ID)
    authority.context = replace(authority.context, action_id=ACTION_ID)
    return authority


def test_a_retryable_failure_does_not_pause_yet(tmp_path: Path) -> None:
    pause_client, base = _PauseChatwoot(), _FoundBuyer(40)
    authority = _native(NATIVE_PURCHASE_PERMISSION)
    authority.step = _step(entry_permission=NATIVE_PURCHASE_PERMISSION, template_name="no_existe_01")
    _dispatch(tmp_path, authority, _pause_reading(pause_client, base))
    assert [f["next_attempt_at"] is not None for f in authority.finalizations] == [True]
    assert base.reads == [] and pause_client.paused == []


def test_a_purchase_step_closed_before_the_send_pauses_the_buyer_found_in_the_base(tmp_path: Path) -> None:
    # E-196 I-2 (D7): la bienvenida no salio y la accion cerro (permanent_failed, sin mas
    # reintentos). Sin flujo nativo, E-100 lo habria pausado igual: se lo busca como al comprar.
    pause_client, base = _PauseChatwoot(), _FoundBuyer(40)
    authority = _last_retry(NATIVE_PURCHASE_PERMISSION)
    authority.step = _step(entry_permission=NATIVE_PURCHASE_PERMISSION, template_name="no_existe_01")
    chatwoot = _dispatch(tmp_path, authority, _pause_reading(pause_client, base))
    assert _templates(chatwoot) == []
    assert [f["outcome"] for f in authority.finalizations] == ["failed_before_request"]
    assert [read["buyer_phone"] for read in base.reads] == [authority.context.buyer_phone]
    assert [call["conversation_id"] for call in pause_client.paused] == [40]
    assert pause_client.notes[0]["idempotency_marker"] == purchase_pause_note_marker(ACTION_ID)


def test_a_cart_step_closed_before_the_send_is_not_paused(tmp_path: Path) -> None:
    pause_client, base = _PauseChatwoot(), _FoundBuyer(40)
    authority = _last_retry("formulario")
    authority.step = _step(entry_permission="formulario", template_name="no_existe_01")
    _dispatch(tmp_path, authority, _pause_reading(pause_client, base))
    assert base.reads == [] and pause_client.paused == []


class _AcceptanceFails(_NativeAuthority):
    async def record_and_finalize_followup_acceptance(self, **kwargs: object) -> object:
        raise SupabaseError("finalize_followup_acceptance_failed: HTTP 400")


def test_a_welcome_delivered_without_a_confirmed_acceptance_still_pauses_its_conversation(
    tmp_path: Path,
) -> None:
    # E-196 I-2: el remitente la entrego y la base no confirmo (acceptance_finalization_failed,
    # delivery_unknown). No hay cola (ni cliente), pero la pausa va a esa conversacion.
    pause_client = _PauseChatwoot()
    authority = _AcceptanceFails(step=_step(entry_permission=NATIVE_PURCHASE_PERMISSION))
    authority.action = replace(authority.action, action_id=ACTION_ID)
    authority.attempt = replace(authority.attempt, action_id=ACTION_ID)
    authority.context = replace(authority.context, action_id=ACTION_ID)
    chatwoot = _dispatch(tmp_path, authority, _maribel_pause(pause_client, inbox_id=11))
    assert len(_templates(chatwoot)) == 1
    assert [f["reason_code"] for f in authority.finalizations] == ["acceptance_finalization_failed"]
    assert chatwoot.labels == []
    assert [call["conversation_id"] for call in pause_client.paused] == [200]


def test_d2_after_the_welcome_only_its_conversation_is_paused(tmp_path: Path) -> None:
    # D2: con la bienvenida aceptada no se lee la base, aunque el comprador tenga otras
    # conversaciones en el inbox (aca, la 40 y la 44 de un carrito anterior).
    pause_client, base = _PauseChatwoot(), _FoundBuyer(40, 44)
    authority = _native(NATIVE_PURCHASE_PERMISSION)
    _dispatch(tmp_path, authority, _pause_reading(pause_client, base))
    assert base.reads == []
    assert [call["conversation_id"] for call in pause_client.paused] == [200]


# -- Los reintentos y el rastro (E-196 I-1, M-1) --------------------------------


class _FlakyChatwoot(_PauseChatwoot):
    def __init__(self, errors: list[Exception]) -> None:
        super().__init__()
        self.errors = list(errors)
        self.attempts = 0

    async def pause_purchased_conversation(self, **kwargs: object) -> str:
        self.attempts += 1
        if self.errors:
            raise self.errors.pop(0)
        self.paused.append(kwargs)
        return "team_assigned"


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://chatwoot.example.test/api/v1/accounts/3/conversations/200/assignments")
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))


def _flaky_pause(chatwoot: _FlakyChatwoot, slept: list[float]) -> PurchaseAgentPause:
    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return replace(_maribel_pause(chatwoot), sleep=sleep)


def _pause_200(pause: PurchaseAgentPause) -> str:
    return asyncio.run(pause.pause_after_native_welcome(
        action_id=ACTION_ID, conversation_id=200, purchased_at=_purchased_at(),
    ))


def test_a_transient_failure_is_retried_and_the_pause_lands() -> None:
    slept: list[float] = []
    chatwoot = _FlakyChatwoot([
        httpx.ConnectError("caido"),
        ChatwootProtocolError("purchase_pause_assignment_not_confirmed"),
    ])
    assert _pause_200(_flaky_pause(chatwoot, slept)) == "paused"
    assert chatwoot.attempts == 3 and slept == [2.0, 5.0]
    [note] = chatwoot.notes
    assert note["idempotency_marker"] == purchase_pause_note_marker(ACTION_ID)


def test_a_pause_that_keeps_failing_leaves_a_note_for_the_team() -> None:
    slept: list[float] = []
    chatwoot = _FlakyChatwoot([_status_error(502), _status_error(503), _status_error(500)])
    assert _pause_200(_flaky_pause(chatwoot, slept)) == "failed"
    assert chatwoot.attempts == 3 and slept == [2.0, 5.0]
    [note] = chatwoot.notes
    assert note["note_body"] == PURCHASE_PAUSE_PENDING_NOTE_BODY
    assert note["idempotency_marker"].startswith(PURCHASE_PAUSE_PENDING_NOTE_MARKER_PREFIX)
    # No se hace pasar por una pausa puesta: la reanudacion no la toma.
    assert not purchase_pause_note_present([{"private": True, "content": note["idempotency_marker"]}])


def test_a_client_error_is_not_retried() -> None:
    slept: list[float] = []
    chatwoot = _FlakyChatwoot([_status_error(404)])
    assert _pause_200(_flaky_pause(chatwoot, slept)) == "failed"
    assert chatwoot.attempts == 1 and slept == []
    assert chatwoot.notes[0]["note_body"] == PURCHASE_PAUSE_PENDING_NOTE_BODY


def test_another_team_keeps_the_label_and_still_gets_the_purchase_note() -> None:
    # M-1: la etiqueta ya quedo puesta; sin la nota, la reanudacion por silencio la levantaria.
    slept: list[float] = []
    chatwoot = _FlakyChatwoot([ChatwootAssignmentConflictError("purchase_pause_team_conflict")])
    assert _pause_200(_flaky_pause(chatwoot, slept)) == "paused"
    assert chatwoot.attempts == 1
    [note] = chatwoot.notes
    assert note["idempotency_marker"] == purchase_pause_note_marker(ACTION_ID)


# -- El flag del runtime --------------------------------------------------------


def test_the_flag_is_off_by_default_and_per_runtime() -> None:
    field = Settings.__dataclass_fields__["purchase_agent_pause_after_native_welcome_enabled"]
    assert field.default is False
    assert (
        "purchase_agent_pause_after_native_welcome_enabled"
        in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    )


def test_the_flag_is_read_from_its_variable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # El arranque de produccion de ATT1 con la pausa y sin el motor: la variable
    # se lee (el arranque frena por ella), y sin la variable arranca como hoy.
    with pytest.raises(ValueError, match="^PURCHASE_AGENT_PAUSE_AFTER_NATIVE_WELCOME_ENABLED requires"):
        _start(monkeypatch, tmp_path / "con", "carrito", extra={
            "PURCHASE_AGENT_PAUSE_ENABLED": "true",
            "PURCHASE_AGENT_PAUSE_AFTER_NATIVE_WELCOME_ENABLED": "true",
        })
    settings, _ = _start(monkeypatch, tmp_path / "sin", "carrito", extra={
        "PURCHASE_AGENT_PAUSE_ENABLED": "true",
    })
    assert settings.purchase_agent_pause_after_native_welcome_enabled is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"purchase_agent_pause_enabled": False},
        {"flow_engine_enabled": False},
    ],
)
def test_the_flag_without_the_pause_or_the_motor_does_not_start(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    fields: dict[str, Any] = {
        "purchase_agent_pause_enabled": True,
        "hotmart_purchase_worker_enabled": True,
        "purchase_agent_pause_after_native_welcome_enabled": True,
    }
    settings = engine_settings(tmp_path, **{**fields, **overrides})
    with pytest.raises(ValueError, match="^PURCHASE_AGENT_PAUSE_AFTER_NATIVE_WELCOME_ENABLED requires"):
        _app(settings)


def test_the_bridge_hands_the_pause_to_the_dispatcher_only_with_the_flag(tmp_path: Path) -> None:
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    on = _app(engine_settings(
        tmp_path / "on",
        purchase_agent_pause_enabled=True,
        hotmart_purchase_worker_enabled=True,
        purchase_agent_pause_after_native_welcome_enabled=True,
    ))
    pause = on.state.durable_dispatcher._purchase_pause_after_native_welcome
    assert isinstance(pause, PurchaseAgentPause)
    # La misma pausa que la del worker: el mismo inbox, team y zona.
    assert pause is on.state.resolution_worker._purchase_agent_pause

    off = _app(engine_settings(
        tmp_path / "off",
        purchase_agent_pause_enabled=True,
        hotmart_purchase_worker_enabled=True,
    ))
    assert off.state.durable_dispatcher._purchase_pause_after_native_welcome is None
    assert off.state.resolution_worker._purchase_agent_pause is not None
