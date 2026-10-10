"""El bridge de ATT1 arranca con el set completo de variables de produccion.

Los demas tests arman ``Settings`` a mano, campo por campo. Aca el camino es el
del contenedor: variables de entorno -> ``build_app()`` (``Settings.from_env()`` y
``create_app``, con el procesador de Hermes que arma la fabrica de uvicorn), con
el manifiesto de ATT1 escrito en disco. Cubre lo que ninguna otra suite
prueba junto: que el set de variables con el que la instancia va a prender cada
flujo (pago fallido, carrito y primer contacto tras el formulario), y los tres a
la vez, pasa todas las validaciones de arranque y arma los workers. Desde la
1.5.0, tambien el seguimiento con cupon (el lugar ``descuento``) junto a los
tres.

Datos:

* ``tests/fixtures/instances/att1/``: el manifiesto y el conocimiento de ATT1. El
  fixture es la copia del 2026-09-28, con todos los flujos apagados, sin el
  evento ``intencion`` y sin ``[adaptadores.ghl]``. Cada test escribe su copia
  con lo que la instancia va a tener al prender el flujo: los flujos en ``true``,
  ``intencion`` en ``eventos`` y la seccion del adaptador de GHL con sus dos
  formularios (los de las capturas de ``tests/fixtures/ghl/``).
* La aceptacion del riesgo del adaptador que se escribe en esa copia es DE
  PRUEBA. Ningun manifiesto real la recibe de este codigo: en la instancia la
  escribe a mano quien firma (cabo LAN-054).
* Los nombres de las plantillas, su idioma y su categoria son los del catalogo
  capturado del inbox 11 (``chatwoot_inbox_11_message_templates_20261001.json``).
  La del cupon no estaba ahi: sale de la captura por la API del 2026-10-10
  (``chatwoot_inbox_11_message_templates_20261010.json``), con el horario que
  decidio Dan ese dia (09-21 de CDMX).
* El barrido del cupon con los clientes reales lee la 21 de ATT1 capturada y
  anonimizada (``chatwoot_followup_candidate_inbox_11_20261010.json``), sobre
  ``httpx.MockTransport``: nada sale a la red.
* El scope y la politica son los que publica la instancia
  (``tests/fixtures/instances/att1/politica-piloto.json``).
* Los valores de las variables secretas son de prueba. La lista de variables es
  la de la seccion (a) del plan de salida a produccion de ATT1 mas las cuatro
  del primer contacto (1.3.0); no se leyo ningun archivo de entorno real.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

import bridge.app as app_module
from bridge.app import Settings
from bridge.chatwoot import ChatwootClient
from bridge.instance_manifest import GHL_ADAPTER_RISK_CONTRACT
from bridge.supabase import SupabaseClient
from test_instance_wiring import _FirstContactAuthority

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
ATT1 = FIXTURES / "instances" / "att1"
PILOT = json.loads((ATT1 / "politica-piloto.json").read_text(encoding="utf-8"))
INBOX_11 = json.loads(
    (FIXTURES / "chatwoot_inbox_11_message_templates_20261001.json").read_text(
        encoding="utf-8"
    )
)
# Los dos formularios de GHL de ATT1 (ads-a y la landing -d), los de las capturas.
GHL_FORMS = ("EgDqRl2xWc59YjVW1q8W", "Om5FpIg5Sr5ce7nSkuPy")
TEST_ACCEPTED_BY = "aceptacion de prueba (tests)"

CART_TEMPLATE = "att1_carrito_abandonado_01"
PAYMENT_FAILURE_TEMPLATE = "att1_compra_fallida_01"
FORM_TEMPLATE = "att1_interes_precheckout_01"
# El catalogo del inbox 11 capturado por la API el 2026-10-10 (F1), con la
# plantilla del cupon.
INBOX_11_COUPON = json.loads(
    (FIXTURES / "chatwoot_inbox_11_message_templates_20261010.json").read_text(
        encoding="utf-8"
    )
)
COUPON_TEMPLATE = "att1_seguimiento_descuento_01"
# La 21 de ATT1 capturada el 2026-10-10 y anonimizada (F4): respondio la
# plantilla del carrito, recibio el link del agente y se callo. Su telefono es
# sintetico, con la forma del wa_id de Mexico (521...).
COUPON_CANDIDATE = json.loads(
    (FIXTURES / "chatwoot_followup_candidate_inbox_11_20261010.json").read_text(
        encoding="utf-8"
    )
)
COUPON_WA_ID = COUPON_CANDIDATE["conversation"]["conversation"]["meta"]["sender"][
    "phone_number"
].removeprefix("+")

OUTBOUND_FLOWS = ("pago_fallido", "carrito", "precheckout")


# ------------------------------------------------------------- el manifiesto


def _write_instance(
    tmp_path: Path, *flows: str, adapter: bool = True, accepted: bool = True
) -> Path:
    """La copia de la instancia con los flujos prendidos, como va a estar en ATT1."""
    target = tmp_path / "instancia"
    shutil.copytree(ATT1, target)
    knowledge = target / "conocimiento" / "knowledge-v1.toml"
    knowledge_text = knowledge.read_text(encoding="utf-8")
    assert 'estado = "borrador"' in knowledge_text
    knowledge.write_text(
        knowledge_text.replace(
            'estado = "borrador"',
            'estado = "aprobado"\naprobado_por = "test"\naprobado_el = 2026-09-28',
        ),
        encoding="utf-8",
    )
    path = target / "instancia.toml"
    text = path.read_text(encoding="utf-8")
    events = 'eventos = ["carrito", "pago_fallido", "compra", "entrante"]'
    assert events in text
    text = text.replace(events, events[:-1] + ', "intencion"]')
    for flow in flows:
        off = f"\n{flow} = false\n"
        assert text.count(off) == 1, flow
        text = text.replace(off, f"\n{flow} = true\n")
    if "descuento" in flows:
        # El lugar descuento con la plantilla del cupon: sin ella, un manifiesto
        # con flujos.descuento prendido no carga.
        cart = f'carrito = {{ nombre = "{CART_TEMPLATE}", idioma = "es_MX" }}\n'
        assert text.count(cart) == 1
        text = text.replace(
            cart, cart + f'descuento = {{ nombre = "{COUPON_TEMPLATE}", idioma = "es_MX" }}\n'
        )
    if adapter:
        text += "\n[adaptadores.ghl]\nformularios = [{}]\n".format(
            ", ".join(f'"{form}"' for form in GHL_FORMS)
        )
        if accepted:
            text += (
                f'riesgo_aceptado_por = "{TEST_ACCEPTED_BY}"\n'
                "riesgo_aceptado_el = 2026-10-01\n"
                f'riesgo_contrato = "{GHL_ADAPTER_RISK_CONTRACT}"\n'
            )
    path.write_text(text, encoding="utf-8")
    return target


# ---------------------------------------------------------------- el entorno

# Toda variable que lee el bridge: se limpian antes de cada test para que el
# entorno de quien corre la suite no cambie el resultado.
_BRIDGE_VARIABLES = sorted(
    set(
        re.findall(
            r'os\.(?:getenv\(|environ\[|environ\.get\()\s*"([A-Z][A-Z0-9_]+)"',
            (ROOT / "src" / "bridge" / "app.py").read_text(encoding="utf-8"),
        )
    )
)


def _base_env(instance: Path) -> dict[str, str]:
    """Lo que el bridge de ATT1 tiene cargado con todos los flujos apagados."""
    return {
        "INSTANCE_MANIFEST_PATH": str(instance / "instancia.toml"),
        "COMMERCIAL_KNOWLEDGE_ENABLED": "true",
        "HERMES_MODEL_NAME": "att1-agente-comercial",
        # El valor de produccion (despliegue/secretos/generar.py de la instancia).
        "HERMES_API_BASE_URL": "http://hermes:8644/v1",
        "HERMES_API_KEY": "test-hermes-key",
        "SUPABASE_BASE_URL": "http://att1-gateway.test",
        "SUPABASE_SERVICE_ROLE_KEY": "test-service-role-key",
        "CHATWOOT_WEBHOOK_SECRET": "test-chatwoot-webhook-secret",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "2",
        "CHATWOOT_INBOX_ID": "11",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_AGENT_BOT_ACCESS_TOKEN": "test-agent-bot-token",
        "CHATWOOT_AGENT_BOT_ID": "7",
        "CHATWOOT_PAUSE_MACRO_ID": "3",
        "CHATWOOT_RESUME_MACRO_ID": "4",
        "MESSAGING_CHANNEL": "waba",
        "ALLOWED_WHATSAPP_JID": "",
        "CAPTURE_DIR": str(instance / "captures"),
        "SHADOW_DIR": str(instance / "shadow"),
        "REPLY_DIR": str(instance / "replies"),
        "META_FINAL_EFFECT_EVIDENCE_DIR": str(instance / "meta-effects"),
        "GIT_SHA": "0123456789abcdef0123456789abcdef01234567",
        # El adaptador de GHL ya admite intenciones en ATT1.
        "GHL_PRECHECKOUT_ADAPTER_ENABLED": "true",
        "GHL_PRECHECKOUT_ADAPTER_TOKEN": "ghl-adapter-test-token-0123456789abcdef",
    }


def _inbound_env() -> dict[str, str]:
    """El entrante completo en modo scoped: va antes de la primera plantilla."""
    return {
        "HERMES_SHADOW_ENABLED": "true",
        "CHATWOOT_AUTOMATED_REPLIES_ENABLED": "true",
        "CHATWOOT_CUT_B_ADMISSION_ENABLED": "true",
        "CHATWOOT_CUT_B_AGENT_ENABLED": "true",
        "CHATWOOT_CUT_B_SCOPE_KEY": "att1-inbound",
        "CHATWOOT_CUT_B_SCOPE_VERSION": "1",
        "CHATWOOT_DURABLE_OPT_OUT_ENABLED": "true",
        "CHATWOOT_OPT_OUT_MACRO_ID": "5",
        "CHATWOOT_OPT_OUT_PROJECTION_WORKER_ID": "att1-opt-out-projection-1",
        "CHATWOOT_HUMAN_PAUSE_ENABLED": "true",
        "HUMAN_HANDOFF_ADMISSION_ENABLED": "true",
        "HUMAN_HANDOFF_PROJECTION_ENABLED": "true",
        "HUMAN_HANDOFF_PROJECTION_WORKER_ID": "att1-handoff-projection-1",
        "HANDOFF_PROJECTION_POLICY_KEY": "att1-derivacion",
        "HANDOFF_PROJECTION_POLICY_VERSION": "1",
        "CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED": "true",
        "PAYMENT_LINK_ENABLED": "true",
    }


def _outbound_env(*, final_effect: bool) -> dict[str, str]:
    """Lo comun a toda salida: Hotmart, la frontera, el dispatcher y la plantilla."""
    scope = PILOT["pilot_scope"]
    policy = PILOT["policy"]
    return {
        "HOTMART_HOTTOK": "test-hottok",
        "PORTABLE_HOTMART_PURCHASE_STOP_ENABLED": "true",
        "LANCEMOS_PILOT_BOUNDARY_ENABLED": "true",
        "LANCEMOS_PILOT_SCOPE_KEY": scope["scope_key"],
        # La v2 (consented_intent_in_cohort) es la que usa el E2E.
        "LANCEMOS_PILOT_SCOPE_VERSION": "2",
        "LANCEMOS_PILOT_TENANT_KEY": "lancemos",
        "LANCEMOS_PILOT_CHANNEL_PROVIDER": scope["channel_provider"],
        "LANCEMOS_PILOT_CHANNEL_ACCOUNT_REF": f"{scope['channel_account_ref_prefix']}11",
        "FOLLOWUP_POLICY_KEY": policy["policy_key"],
        "FOLLOWUP_POLICY_VERSION": str(policy["version"]),
        "DURABLE_DISPATCHER_ENABLED": "true",
        "DURABLE_DISPATCHER_WORKER_ID": "att1-dispatcher-1",
        "DURABLE_OUTBOUND_ENABLED": "true",
        "DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED": "true",
        "WABA_FIRST_TOUCH_TEMPLATE_NAME": CART_TEMPLATE,
        "WABA_TEMPLATE_LANGUAGE": "es_MX",
        "WABA_TEMPLATE_CATEGORY": "MARKETING",
        "LEAD_FIRST_NAME_GREETING_ENABLED": "true",
        "META_FINAL_EFFECT_ENABLED": "true" if final_effect else "false",
    }


# El worker de resolucion (y el de compras, que cuelga de el) planifica desde
# los eventos de Hotmart: lo necesitan el pago fallido y el carrito. El primer
# contacto no: lo planifica la admision del formulario, y la compra lo frena en
# su propia admision (PORTABLE_HOTMART_PURCHASE_STOP_ENABLED).
_FLOW_ENV: dict[str, dict[str, str]] = {
    "pago_fallido": {
        "RESOLUTION_WORKER_ENABLED": "true",
        "HOTMART_PURCHASE_WORKER_ENABLED": "true",
        "PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED": "true",
        "WABA_PAYMENT_FAILURE_TEMPLATE_NAME": PAYMENT_FAILURE_TEMPLATE,
    },
    "carrito": {
        "RESOLUTION_WORKER_ENABLED": "true",
        "HOTMART_PURCHASE_WORKER_ENABLED": "true",
        "PORTABLE_HOTMART_RECOVERY_ENABLED": "true",
    },
    "precheckout": {
        "PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED": "true",
        "LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY": (
            PILOT["first_contact"]["pilot_scope"]["scope_key"]
        ),
        "LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION": str(
            PILOT["first_contact"]["pilot_scope"]["version"]
        ),
        "WABA_PRECHECKOUT_TEMPLATE_NAME": FORM_TEMPLATE,
    },
    # El seguimiento con cupon no pasa por el dispatcher ni por el piloto: le
    # alcanzan el entrante scoped (Corte B, el AgentBot y Supabase) y sus
    # variables. El horario es el de las otras plantillas de ATT1; las paginas
    # y el intervalo, los que la spec del cupon le suma a la instancia (sin
    # ellas arranca con los defaults, ver abajo).
    "descuento": {
        "CONVERSATION_FOLLOWUP_ENABLED": "true",
        "CONVERSATION_FOLLOWUP_TEMPLATE_NAME": COUPON_TEMPLATE,
        "CONVERSATION_FOLLOWUP_TEMPLATE_LANGUAGE": "es_MX",
        "CONVERSATION_FOLLOWUP_COUPON_CODE": "TIROIDES10",
        "CONVERSATION_FOLLOWUP_PRODUCT_NAME": "Alimenta tu Tiroides",
        "CONVERSATION_FOLLOWUP_SEND_HOURS": "09-21",
        "CONVERSATION_FOLLOWUP_MAX_PAGES": "10",
        "CONVERSATION_FOLLOWUP_INTERVAL_SECONDS": "600",
    },
}


def _production_env(
    instance: Path, *flows: str, final_effect: bool = True
) -> dict[str, str]:
    env = {**_base_env(instance), **_inbound_env()}
    if flows:
        env.update(_outbound_env(final_effect=final_effect))
    for flow in flows:
        env.update(_FLOW_ENV[flow])
    return env


def _load(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> Settings:
    for name in _BRIDGE_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings.from_env()


def _build_app() -> object:
    # La fabrica que corre uvicorn en el contenedor, sin clientes inyectados: el
    # bridge arma el de Chatwoot, el de Supabase y el de Hermes con sus
    # variables. Nada sale a la red al construir.
    return app_module.build_app()


def _start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *flows: str,
    final_effect: bool = True,
    drop: tuple[str, ...] = (),
    extra: dict[str, str] | None = None,
    accepted: bool = True,
) -> tuple[Settings, object]:
    instance = _write_instance(tmp_path, "inbound", *flows, accepted=accepted)
    env = _production_env(instance, *flows, final_effect=final_effect)
    for name in drop:
        assert name in env, name
        del env[name]
    env.update(extra or {})
    settings = _load(monkeypatch, env)
    return settings, _build_app()


# ------------------------------------------------------- lo que tiene que pasar


def test_the_variable_list_of_the_bridge_is_read_from_its_source() -> None:
    # Si from_env cambia la forma de leer el entorno y la expresion de arriba
    # deja de encontrar las variables, los tests de abajo ya no limpian nada.
    assert len(_BRIDGE_VARIABLES) > 150
    for name in (
        "INSTANCE_MANIFEST_PATH",
        "PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED",
        "LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY",
        "WABA_PRECHECKOUT_TEMPLATE_NAME",
        "HERMES_API_KEY",
    ):
        assert name in _BRIDGE_VARIABLES


def test_the_set_names_only_variables_the_bridge_reads(tmp_path: Path) -> None:
    instance = tmp_path / "instancia"
    env = _production_env(instance, *OUTBOUND_FLOWS, "descuento")

    assert sorted(set(env) - set(_BRIDGE_VARIABLES)) == []


def test_the_coupon_variables_are_the_ones_of_the_captured_catalog() -> None:
    by_name = {template["name"]: template for template in INBOX_11_COUPON["message_templates"]}
    env = _FLOW_ENV["descuento"]

    captured = by_name[env["CONVERSATION_FOLLOWUP_TEMPLATE_NAME"]]
    assert captured["status"] == "APPROVED"
    assert captured["language"] == env["CONVERSATION_FOLLOWUP_TEMPLATE_LANGUAGE"]
    # El producto es el {{2}} del ejemplo que aprobo Meta, y el de la instancia.
    [body] = [c for c in captured["components"] if c["type"] == "BODY"]
    assert body["example"]["body_text"][0][1] == env["CONVERSATION_FOLLOWUP_PRODUCT_NAME"]
    assert f'product_name = "{env["CONVERSATION_FOLLOWUP_PRODUCT_NAME"]}"' in (
        ATT1 / "instancia.toml"
    ).read_text(encoding="utf-8")


def test_the_template_variables_are_the_ones_of_the_captured_inbox_11_catalog() -> None:
    by_name = {template["name"]: template for template in INBOX_11["templates"]}
    env = {**_outbound_env(final_effect=True)}
    for flow in OUTBOUND_FLOWS:
        env.update(_FLOW_ENV[flow])

    for variable in (
        "WABA_FIRST_TOUCH_TEMPLATE_NAME",
        "WABA_PAYMENT_FAILURE_TEMPLATE_NAME",
        "WABA_PRECHECKOUT_TEMPLATE_NAME",
    ):
        captured = by_name[env[variable]]
        assert captured["status"] == "APPROVED"
        assert captured["language"] == env["WABA_TEMPLATE_LANGUAGE"]
        # Las tres son de la misma categoria: no hace falta
        # WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY.
        assert captured["category"] == env["WABA_TEMPLATE_CATEGORY"]
    assert "WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY" not in env


def test_att1_with_every_flow_off_starts_as_today(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Hoy: adaptador de GHL prendido, sin la aceptacion, y ningun flujo.
    instance = _write_instance(tmp_path, accepted=False)
    settings = _load(monkeypatch, _base_env(instance))

    app = _build_app()

    assert settings.instance_manifest is not None
    assert not any(settings.instance_manifest.flows.values())
    assert settings.instance_manifest.ghl_risk_acceptance is None
    assert app.state.durable_dispatcher is None  # type: ignore[attr-defined]
    assert app.state.resolution_worker is None  # type: ignore[attr-defined]


def test_the_scoped_inbound_starts_before_any_outbound_flow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, app = _start(monkeypatch, tmp_path)

    assert settings.allowed_jid is None
    assert settings.chatwoot_scoped_inbound_senders_enabled is True
    assert settings.chatwoot_durable_opt_out_enabled is True
    assert settings.payment_link_enabled is True
    assert app.state.opt_out_projection_worker is not None  # type: ignore[attr-defined]
    assert app.state.human_handoff_projection_worker is not None  # type: ignore[attr-defined]
    assert app.state.durable_dispatcher is None  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "flows",
    [("pago_fallido",), ("carrito",), ("precheckout",), OUTBOUND_FLOWS],
    ids=["pago_fallido", "carrito", "precheckout", "the three together"],
)
def test_each_flow_starts_with_its_production_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flows: tuple[str, ...]
) -> None:
    settings, app = _start(monkeypatch, tmp_path, *flows)

    manifest = settings.instance_manifest
    assert manifest is not None
    assert {flow for flow, on in manifest.flows.items() if on} == {"inbound", *flows}
    assert manifest.ghl_risk_acceptance is not None
    assert settings.portable_hotmart_payment_failure_enabled is ("pago_fallido" in flows)
    assert settings.portable_hotmart_recovery_enabled is ("carrito" in flows)
    assert settings.portable_precheckout_first_contact_enabled is ("precheckout" in flows)
    assert settings.meta_final_effect_enabled is True
    assert settings.allowed_jid is None

    dispatcher = app.state.durable_dispatcher  # type: ignore[attr-defined]
    assert dispatcher is not None
    # El worker de resolucion planifica desde los eventos de Hotmart; el primer
    # contacto lo planifica la admision del formulario, sin worker.
    hotmart_flow = bool({"pago_fallido", "carrito"} & set(flows))
    assert (app.state.resolution_worker is not None) is hotmart_flow  # type: ignore[attr-defined]

    template = app_module._waba_template_config(settings)
    assert template is not None
    assert template.language == "es_MX"
    assert template.first_touch_name_for(trigger_kind="precheckout_intent") == (
        FORM_TEMPLATE if "precheckout" in flows else None
    )
    if "pago_fallido" in flows:
        assert template.first_touch_name_for(trigger_kind="payment_failure") == (
            PAYMENT_FAILURE_TEMPLATE
        )
    if "carrito" in flows:
        assert template.first_touch_name_for(trigger_kind="cart_abandonment") == (
            CART_TEMPLATE
        )


def test_the_first_name_inference_starts_with_the_production_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # El constructor real corre (es el que valida la URL): el espia solo lo mira.
    built: list[dict[str, object]] = []
    real_client = app_module.FirstNameInferenceClient

    def spy(**kwargs: object) -> object:
        client = real_client(**kwargs)  # type: ignore[arg-type]
        built.append(kwargs)
        return client

    monkeypatch.setattr(app_module, "FirstNameInferenceClient", spy)

    settings, app = _start(
        monkeypatch,
        tmp_path,
        *OUTBOUND_FLOWS,
        extra={"LEAD_FIRST_NAME_INFERENCE_ENABLED": "true"},
    )

    assert app is not None
    assert settings.lead_first_name_inference_enabled is True
    # Sin LEAD_FIRST_NAME_MODEL_NAME, el modelo es el profile del agente de ATT1,
    # por el API server de Hermes de la instancia, con el valor de produccion.
    assert settings.lead_first_name_model_name == "att1-agente-comercial"
    [kwargs] = built
    assert kwargs["base_url"] == "http://hermes:8644/v1"
    assert kwargs["model_name"] == "att1-agente-comercial"


def test_the_first_name_inference_does_not_start_without_a_form_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Sin /webhooks/lead ni el adaptador de GHL nada la dispara: se rechaza, igual
    # que el saludo sin el modo directo. Con el carrito solo, porque el primer
    # contacto ya exige una de las dos entradas.
    with pytest.raises(
        ValueError,
        match=(
            "LEAD_FIRST_NAME_INFERENCE_ENABLED in a portable runtime requires "
            "LEAD_PRECHECKOUT_ENABLED or GHL_PRECHECKOUT_ADAPTER_ENABLED"
        ),
    ):
        _start(
            monkeypatch,
            tmp_path,
            "carrito",
            drop=("GHL_PRECHECKOUT_ADAPTER_ENABLED", "GHL_PRECHECKOUT_ADAPTER_TOKEN"),
            extra={"LEAD_FIRST_NAME_INFERENCE_ENABLED": "true"},
        )


def test_the_first_name_inference_does_not_start_without_the_greeting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="lead_first_name_configuration_incomplete"):
        _start(
            monkeypatch,
            tmp_path,
            *OUTBOUND_FLOWS,
            drop=("LEAD_FIRST_NAME_GREETING_ENABLED",),
            extra={"LEAD_FIRST_NAME_INFERENCE_ENABLED": "true"},
        )


def test_the_final_effect_can_stay_closed_until_the_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # META_FINAL_EFFECT_ENABLED es lo ultimo que se prende: con todo lo demas
    # cargado y el gate cerrado, el bridge arranca igual.
    settings, app = _start(monkeypatch, tmp_path, *OUTBOUND_FLOWS, final_effect=False)

    assert settings.meta_final_effect_enabled is False
    assert app.state.durable_dispatcher is not None  # type: ignore[attr-defined]


def test_the_three_flows_wire_the_whatsapp_equivalence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Con un flujo portable de salida, el dispatcher y su sender comparan 52/521
    # y 54/549 como el mismo movil (solo con manifiesto).
    _, app = _start(monkeypatch, tmp_path, *OUTBOUND_FLOWS)
    dispatcher = app.state.durable_dispatcher  # type: ignore[attr-defined]

    assert dispatcher._whatsapp_equivalence_enabled is True
    assert dispatcher._sender._whatsapp_equivalence_enabled is True


# ------------------------------------------------- el seguimiento con cupon


def _spy_followup_sweeper(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    # El constructor real corre: el espia solo mira lo que recibe.
    built: list[dict[str, object]] = []
    real_sweeper = app_module.ConversationFollowupSweeper

    def spy(**kwargs: object) -> object:
        built.append(kwargs)
        return real_sweeper(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(app_module, "ConversationFollowupSweeper", spy)
    return built


def test_the_coupon_starts_with_the_three_flows_and_its_production_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    built = _spy_followup_sweeper(monkeypatch)

    settings, app = _start(monkeypatch, tmp_path, *OUTBOUND_FLOWS, "descuento")

    manifest = settings.instance_manifest
    assert manifest is not None
    assert {flow for flow, on in manifest.flows.items() if on} == {
        "inbound",
        *OUTBOUND_FLOWS,
        "descuento",
    }
    assert app.state.conversation_followup_sweeper is not None  # type: ignore[attr-defined]
    [kwargs] = built
    assert kwargs["template_name"] == COUPON_TEMPLATE
    assert kwargs["coupon_code"] == "TIROIDES10"
    assert (kwargs["max_pages"], kwargs["scan_interval_seconds"]) == (10, 600.0)
    # La identidad como el entrante, la reserva portable y el filtro del nombre.
    assert callable(kwargs["external_user_id_resolver"])
    assert kwargs["phone_equivalence"] is True
    assert kwargs["refuse_unsafe_greeting"] is True
    # De 09 a 21 de CDMX, la zona del manifiesto, y a todo el inbox: con los
    # remitentes acotados por scope no hay un JID que lo limite.
    assert kwargs["send_hours"] == (9, 21)
    assert kwargs["time_zone"] == ZoneInfo(manifest.time_zone) == ZoneInfo("America/Mexico_City")
    assert kwargs["only_phone"] is None
    assert kwargs["allowed_phone"] is None


def test_the_coupon_without_its_pages_and_interval_starts_with_the_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Fija lo que hace hoy el arranque, no lo que conviene: las paginas y el
    # intervalo tienen default (5 paginas de 25 por estado, 300 s), asi que
    # sacarlas no frena el bridge. Si el arranque pasa a exigirlas con el
    # manifiesto, este test se invierte.
    built = _spy_followup_sweeper(monkeypatch)

    _start(
        monkeypatch,
        tmp_path,
        *OUTBOUND_FLOWS,
        "descuento",
        drop=("CONVERSATION_FOLLOWUP_MAX_PAGES", "CONVERSATION_FOLLOWUP_INTERVAL_SECONDS"),
    )

    [kwargs] = built
    assert (kwargs["max_pages"], kwargs["scan_interval_seconds"]) == (5, 300.0)


@pytest.mark.parametrize(
    ("drop", "message"),
    [
        (
            "CONVERSATION_FOLLOWUP_TEMPLATE_NAME",
            "CONVERSATION_FOLLOWUP_TEMPLATE_NAME must match plantillas.descuento.nombre",
        ),
        (
            "CONVERSATION_FOLLOWUP_TEMPLATE_LANGUAGE",
            "CONVERSATION_FOLLOWUP_TEMPLATE_LANGUAGE must match plantillas.descuento.idioma",
        ),
        (
            "CONVERSATION_FOLLOWUP_COUPON_CODE",
            "CONVERSATION_FOLLOWUP_ENABLED requires .*CONVERSATION_FOLLOWUP_COUPON_CODE",
        ),
        (
            "CONVERSATION_FOLLOWUP_PRODUCT_NAME",
            "CONVERSATION_FOLLOWUP_PRODUCT_NAME must match hotmart.product_name",
        ),
        (
            "CONVERSATION_FOLLOWUP_SEND_HOURS",
            "CONVERSATION_FOLLOWUP_SEND_HOURS is required with an instance manifest",
        ),
    ],
)
def test_a_missing_coupon_variable_does_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, drop: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _start(monkeypatch, tmp_path, *OUTBOUND_FLOWS, "descuento", drop=(drop,))


def test_the_coupon_variables_without_its_flow_in_the_manifest_do_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Las variables del cupon cargadas antes de mergear el manifiesto con
    # flujos.descuento = true.
    instance = _write_instance(tmp_path, "inbound", *OUTBOUND_FLOWS)
    _load(monkeypatch, _production_env(instance, *OUTBOUND_FLOWS, "descuento"))

    with pytest.raises(ValueError, match="conversation_followup_enabled->descuento"):
        _build_app()


# ------------------------------- un barrido del cupon con los clientes reales
#
# Los dobles de tests/test_followup_discount.py no pasan por los clientes
# reales, y lo que manda ATT1 a la reserva portable es justo una costura entre
# los dos lados: la kwarg phone_equivalence de SupabaseClient y la firma del
# resolvedor del entrante (wa_id, conversation_id=...). Aca corre un barrido
# completo del barredor que arma build_app() con el set de produccion, contra
# el ChatwootClient y el SupabaseClient de verdad sobre httpx.MockTransport.
# Chatwoot sirve lo capturado: el catalogo del inbox 11 (F1) y la 21 con su
# pagina (F4), reducida a la 21 porque las otras 24 conversaciones no estan
# capturadas. PostgREST contesta con la forma de las filas de nuestras RPC, que
# no son un borde externo. Lo unico fijo del barredor son el reloj y el ULID.

# 30 h despues de que la persona de la 21 contesto: el 10/10 a las 16:15 de
# CDMX, adentro de 09-21.
_COUPON_NOW = (
    max(
        message["created_at"]
        for message in COUPON_CANDIDATE["conversation"]["messages"]
        if message["message_type"] == 0
    )
    + 30 * 3_600
)
_COUPON_ULID = "01K5ABCDEFX2VYB4M6X9CDPTF1"
_COUPON_EVENT_ID = "00000000-0000-0000-0000-000000000601"
_COUPON_ISSUANCE_ID = "00000000-0000-0000-0000-000000000602"
# El link que emite la reserva: el del agente en la 21, con el ULID nuevo.
_COUPON_AGENT_LINK = next(
    word
    for word in COUPON_CANDIDATE["conversation"]["messages"][-1]["content"].split()
    if word.startswith("https://pay.hotmart.com/")
)
_COUPON_ISSUED_LINK = re.sub(
    r"~hermes~v1~[0-9A-Z]{26}", f"~hermes~v1~{_COUPON_ULID}", _COUPON_AGENT_LINK
)
_COUPON_SENT_MESSAGE_ID = 2_800


def _coupon_chatwoot(seen: list[httpx.Request], unexpected: list[str]):
    """Chatwoot de la cuenta 2, inbox 11, con lo capturado en F1 y F4."""
    captured = COUPON_CANDIDATE["conversation"]
    pages = json.loads(json.dumps(COUPON_CANDIDATE["pages"]))
    pages["open"]["payload"] = [
        item for item in pages["open"]["payload"] if item["id"] == 21
    ]
    pages["open"]["meta"]["all_count"] = 1
    prefix = "/api/v1/accounts/2"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if request.method == "GET" and path == f"{prefix}/inboxes/11":
            return httpx.Response(200, json=INBOX_11_COUPON)
        if request.method == "GET" and path == f"{prefix}/conversations":
            params = request.url.params
            page = pages[params["status"]]
            if params["page"] != "1":
                return httpx.Response(
                    200, json={"data": {"payload": [], "meta": page["meta"]}}
                )
            return httpx.Response(200, json={"data": page})
        if request.method == "GET" and path == f"{prefix}/conversations/21":
            return httpx.Response(200, json=captured["conversation"])
        if request.method == "GET" and path == f"{prefix}/conversations/21/messages":
            return httpx.Response(200, json={"payload": captured["messages"]})
        if request.method == "POST" and path == f"{prefix}/conversations/21/messages":
            # Lo que devuelve Chatwoot al aceptar un mensaje del AgentBot: el
            # cliente real lo valida antes de dar el envio por hecho.
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "id": _COUPON_SENT_MESSAGE_ID,
                    "conversation_id": 21,
                    "message_type": 1,
                    "private": False,
                    "content_attributes": body["content_attributes"],
                    "sender": {"type": "agent_bot", "id": 7},
                },
            )
        unexpected.append(f"{request.method} {path}")
        return httpx.Response(599)

    return handler


def _coupon_postgrest(seen: list[httpx.Request], unexpected: list[str]):
    """La base de ATT1: el caso de la 21 nacio del formulario, con 52..."""
    stored = "52" + COUPON_WA_ID[3:]
    rows: dict[tuple[str, str], list[dict[str, object]]] = {
        ("GET", "/rest/v1/channel_identities"): [
            {
                "id": f"identity-{stored}",
                "contact_id": f"contact-{stored}",
                "external_user_id": stored,
                "metadata": {"inbox_id": 11},
            }
        ],
        # Sin inferencia guardada para ese nombre: el saludo es la regla.
        ("POST", "/rest/v1/rpc/get_lead_first_name_inference_v1"): [],
        ("POST", "/rest/v1/rpc/claim_portable_conversation_followup_v1"): [
            {
                "outcome": "claimed",
                "followup_event_id": _COUPON_EVENT_ID,
                "checkout_issuance_id": _COUPON_ISSUANCE_ID,
                "checkout_url_final": _COUPON_ISSUED_LINK,
                "sck_value": f"SCK_ANUNCIO_REDACTADO~hermes~v1~{_COUPON_ULID}",
            }
        ],
        ("POST", "/rest/v1/rpc/authorize_chatwoot_checkout_issuance_v2"): [
            {
                "outcome": "request_started",
                "issuance_id": _COUPON_ISSUANCE_ID,
                "status": "request_started",
            }
        ],
        ("POST", "/rest/v1/rpc/finalize_chatwoot_checkout_issuance_v2"): [
            {
                "outcome": "finalized",
                "issuance_id": _COUPON_ISSUANCE_ID,
                "status": "accepted_by_chatwoot",
            }
        ],
        ("POST", "/rest/v1/rpc/settle_conversation_followup_v1"): [
            {"outcome": "settled", "followup_event_id": _COUPON_EVENT_ID}
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = (request.method, request.url.path)
        if key not in rows:
            unexpected.append(f"{request.method} {request.url.path}")
            return httpx.Response(599)
        return httpx.Response(200, json=rows[key])

    return handler


def test_a_coupon_sweep_through_the_real_clients_claims_with_the_portable_rpc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chatwoot_seen: list[httpx.Request] = []
    postgrest_seen: list[httpx.Request] = []
    unexpected: list[str] = []
    real_create_app = app_module.create_app
    real_sweeper = app_module.ConversationFollowupSweeper

    def create_app_on_mock_transports(settings: Settings, **kwargs: object) -> object:
        # Los clientes que arma create_app con estas variables, con el
        # transporte reemplazado: nada sale a la red.
        chatwoot = ChatwootClient(
            base_url=str(settings.chatwoot_base_url),
            account_id=int(settings.chatwoot_account_id or 0),
            access_token=str(settings.chatwoot_control_api_access_token),
            allowed_jid=settings.allowed_jid,
            agent_bot_access_token=settings.chatwoot_agent_bot_access_token,
            agent_bot_id=settings.agent_bot_id,
            reply_dir=settings.reply_dir,
            pause_macro_id=settings.chatwoot_pause_macro_id,
            resume_macro_id=settings.chatwoot_resume_macro_id,
            opt_out_macro_id=settings.chatwoot_opt_out_macro_id,
            transport=httpx.MockTransport(_coupon_chatwoot(chatwoot_seen, unexpected)),
        )
        supabase = SupabaseClient(
            base_url=str(settings.supabase_base_url),
            service_role_key=str(settings.supabase_service_role_key),
            transport=httpx.MockTransport(_coupon_postgrest(postgrest_seen, unexpected)),
        )
        return real_create_app(
            settings, chatwoot_client=chatwoot, supabase_client=supabase, **kwargs
        )

    def sweeper_with_a_fixed_clock(**kwargs: object) -> object:
        return real_sweeper(
            **kwargs, clock=lambda: _COUPON_NOW, ulid_factory=lambda: _COUPON_ULID
        )

    monkeypatch.setattr(app_module, "create_app", create_app_on_mock_transports)
    monkeypatch.setattr(
        app_module, "ConversationFollowupSweeper", sweeper_with_a_fixed_clock
    )

    _, app = _start(monkeypatch, tmp_path, *OUTBOUND_FLOWS, "descuento")
    sweeper = app.state.conversation_followup_sweeper  # type: ignore[attr-defined]

    assert asyncio.run(sweeper.run_once()) == 1
    assert unexpected == []
    assert (sweeper.last_scan_state, sweeper.last_scan_summary) == (
        "healthy",
        "scanned=1 sent=1",
    )

    # La base: el saludo, la identidad, la reserva portable, la autorizacion,
    # el cierre de la emision y el del seguimiento, en ese orden.
    assert [request.url.path for request in postgrest_seen] == [
        "/rest/v1/rpc/get_lead_first_name_inference_v1",
        "/rest/v1/channel_identities",
        "/rest/v1/rpc/claim_portable_conversation_followup_v1",
        "/rest/v1/rpc/authorize_chatwoot_checkout_issuance_v2",
        "/rest/v1/rpc/finalize_chatwoot_checkout_issuance_v2",
        "/rest/v1/rpc/settle_conversation_followup_v1",
    ]
    lookup, claim, authorize, finalize, settle = (
        postgrest_seen[1],
        *[json.loads(request.content) for request in postgrest_seen[2:]],
    )
    # El resolvedor del entrante busca las dos formas del wa_id de la 21 en la
    # cuenta del manifiesto, y devuelve la guardada (52...).
    assert lookup.url.params["account_id"] == "eq.chatwoot:2"
    assert lookup.url.params["external_user_id"] == (
        f"in.(52{COUPON_WA_ID[3:]},{COUPON_WA_ID})"
    )
    # La reserva portable, con la identidad resuelta: con el wa_id textual
    # (521...) la base no encuentra el caso del formulario y el cupon no sale.
    assert claim == {
        "p_external_conversation_id": 21,
        "p_chatwoot_account_id": 2,
        "p_chatwoot_inbox_id": 11,
        "p_external_user_id": "525500000021",
        "p_contact_email": "lead21@example.com",
        "p_command_key": "followup:21:2766",
        "p_regime": "link_sent_no_purchase",
        "p_template_name": COUPON_TEMPLATE,
        "p_template_language": "es_MX",
        "p_coupon_code": "TIROIDES10",
        "p_last_inbound_message_id": 2765,
        "p_last_outbound_message_id": 2766,
        "p_inbound_age_seconds": 30 * 3_600,
        "p_issuance_ulid": _COUPON_ULID,
    }
    # La autorizacion, con la misma identidad y el mismo ancla.
    assert {
        key: authorize[key]
        for key in ("p_issuance_id", "p_external_user_id", "p_trigger_external_message_id")
    } == {
        "p_issuance_id": _COUPON_ISSUANCE_ID,
        "p_external_user_id": "525500000021",
        "p_trigger_external_message_id": "2766",
    }
    assert (finalize["p_status"], finalize["p_chatwoot_message_id"]) == (
        "accepted_by_chatwoot",
        _COUPON_SENT_MESSAGE_ID,
    )
    assert (settle["p_status"], settle["p_provider_message_id"]) == (
        "sent",
        _COUPON_SENT_MESSAGE_ID,
    )

    # Chatwoot: abiertas y resueltas del inbox 11, y la 21 leida dos veces (en
    # el listado y justo antes de reservar).
    assert [
        (request.url.params["status"], request.url.params["inbox_id"], request.url.params["page"])
        for request in chatwoot_seen
        if request.url.path == "/api/v1/accounts/2/conversations"
    ] == [("open", "11", "1"), ("resolved", "11", "1")]
    assert [
        request.method
        for request in chatwoot_seen
        if request.url.path == "/api/v1/accounts/2/conversations/21/messages"
    ] == ["GET", "GET", "POST"]
    # La plantilla de ATT1 sale como el AgentBot, con dos parametros en el
    # cuerpo y el cupon solo en el boton, sobre el link emitido.
    [sent] = [request for request in chatwoot_seen if request.method == "POST"]
    assert sent.headers["api_access_token"] == "test-agent-bot-token"
    body = json.loads(sent.content)
    assert body["content_attributes"] == {
        "conversation_followup_command_key": "followup:21:2766"
    }
    assert body["template_params"]["name"] == COUPON_TEMPLATE
    assert body["template_params"]["language"] == "es_MX"
    assert body["template_params"]["processed_params"] == {
        "body": {"1": "Lucia", "2": "Alimenta tu Tiroides"},
        "buttons": [
            {
                "type": "url",
                "parameter": _COUPON_ISSUED_LINK.removeprefix("https://pay.hotmart.com/")
                + "&offDiscount=TIROIDES10",
            }
        ],
    }
    assert body["content"].startswith("Hola, Lucia. ")
    assert "TIROIDES10" not in body["content"]


# -------------------------------------------------------------- /ready


class _ReadyAuthority(_FirstContactAuthority):
    """La base que consulta /ready, con los dos scopes publicados y armados."""

    def __init__(self) -> None:
        super().__init__(runtime_state="armed", reason_code="pilot_runtime_armed")
        self.audience_reads: list[tuple[str, int]] = []

    async def get_pilot_runtime_status(self, *, pilot_boundary: object) -> object:
        status = await super().get_pilot_runtime_status(pilot_boundary=pilot_boundary)
        status.runtime_state = "armed"
        status.reason_code = "pilot_runtime_armed"
        return status

    async def get_pilot_scope_audience_mode(
        self, *, scope_key: str, scope_version: int
    ) -> str:
        self.audience_reads.append((scope_key, scope_version))
        return "consented_intent_in_cohort"


def _ready(app: object) -> httpx.Response:
    async def get() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/ready")

    return asyncio.run(get())


def test_ready_reports_the_three_flows_and_the_accepted_risk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    instance = _write_instance(tmp_path, "inbound", *OUTBOUND_FLOWS)
    _load(monkeypatch, _production_env(instance, *OUTBOUND_FLOWS))
    authority = _ReadyAuthority()
    # La misma fabrica, con la base reemplazada por el doble que contesta /ready.
    real_create_app = app_module.create_app
    monkeypatch.setattr(
        app_module,
        "create_app",
        lambda settings, **kwargs: real_create_app(
            settings, supabase_client=authority, **kwargs  # type: ignore[arg-type]
        ),
    )

    response = _ready(_build_app())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ghl_adapter_risk"] == f"accepted:2026-10-01:{GHL_ADAPTER_RISK_CONTRACT}"
    assert body["ghl_precheckout_adapter"] == f"enabled:{len(GHL_FORMS)}-forms"
    assert body["portable_precheckout_first_contact"] == "armed"
    assert TEST_ACCEPTED_BY not in response.text
    # El scope del formulario que consulta es el de sus variables, no el de
    # recuperacion.
    [boundary] = authority.first_contact_boundaries
    assert (boundary.scope_key, boundary.scope_version) == (  # type: ignore[attr-defined]
        PILOT["first_contact"]["pilot_scope"]["scope_key"],
        PILOT["first_contact"]["pilot_scope"]["version"],
    )
    # Con la aceptacion escrita no hace falta leer la audiencia del scope.
    assert authority.audience_reads == []


# El modo de prueba del E2E (paso 9 de Dan): el flag, su telefono y 15 minutos
# en vez de 24 h. Sintetico, con la forma del wa_id de Mexico.
_COUPON_TEST_MODE = {
    "CONVERSATION_FOLLOWUP_ONLY_PHONE": "+5215500000099",
    "CONVERSATION_FOLLOWUP_MIN_AGE_SECONDS": "900",
}


@pytest.mark.parametrize(
    ("extra", "audience", "min_age"),
    [(_COUPON_TEST_MODE, "only_phone", "900"), ({}, "inbox", "86400")],
    ids=["test mode", "opened"],
)
def test_ready_reports_the_coupon_audience_and_hours(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    extra: dict[str, str],
    audience: str,
    min_age: str,
) -> None:
    instance = _write_instance(tmp_path, "inbound", *OUTBOUND_FLOWS, "descuento")
    _load(monkeypatch, {**_production_env(instance, *OUTBOUND_FLOWS, "descuento"), **extra})
    authority = _ReadyAuthority()
    real_create_app = app_module.create_app
    monkeypatch.setattr(
        app_module,
        "create_app",
        lambda settings, **kwargs: real_create_app(
            settings, supabase_client=authority, **kwargs  # type: ignore[arg-type]
        ),
    )

    response = _ready(_build_app())

    assert response.status_code == 200, response.text
    body = response.json()
    # Sin lifespan el barredor no corrio: el estado es el inicial.
    assert body["conversation_followup"] == "never"
    # Con los remitentes por scope, sin el modo de prueba es todo el inbox.
    assert body["conversation_followup_audience"] == audience
    # Abrir se verifica por presencia: 86400, no solo la ausencia del modo.
    assert body["conversation_followup_min_age_seconds"] == min_age
    assert body["conversation_followup_send_hours"] == "09-21"
    assert body["portable_precheckout_first_contact"] == "armed"
    # El telefono de prueba no sale en ninguna forma.
    assert "5500000099" not in response.text


def test_opening_with_the_test_silence_left_does_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # El paso de abrir a medias: se saca CONVERSATION_FOLLOWUP_ONLY_PHONE y
    # queda CONVERSATION_FOLLOWUP_MIN_AGE_SECONDS=900 (por ejemplo, por borrar
    # una sola linea). Arrancado, mandaria la plantilla MARKETING con el 10 % a
    # todo el inbox a los 15 minutos, dentro de la ventana de 24 h, y /ready
    # diria inbox. No arranca, y el mensaje nombra las dos variables.
    with pytest.raises(
        ValueError,
        match=(
            "^CONVERSATION_FOLLOWUP_MIN_AGE_SECONDS below 86400 requires "
            "CONVERSATION_FOLLOWUP_ONLY_PHONE with an instance manifest"
        ),
    ):
        _start(
            monkeypatch,
            tmp_path,
            *OUTBOUND_FLOWS,
            "descuento",
            extra={
                "CONVERSATION_FOLLOWUP_MIN_AGE_SECONDS": (
                    _COUPON_TEST_MODE["CONVERSATION_FOLLOWUP_MIN_AGE_SECONDS"]
                )
            },
        )


# ------------------------------------------------- lo que no tiene que arrancar


@pytest.mark.parametrize(
    ("flows", "drop", "message"),
    [
        (("precheckout",), "HOTMART_HOTTOK", "HOTMART_HOTTOK"),
        (("carrito",), "LANCEMOS_PILOT_BOUNDARY_ENABLED", "LANCEMOS_PILOT_BOUNDARY_ENABLED"),
        (("carrito",), "WABA_FIRST_TOUCH_TEMPLATE_NAME", "WABA_FIRST_TOUCH_TEMPLATE_NAME"),
        (("carrito",), "FOLLOWUP_POLICY_KEY", "FOLLOWUP_POLICY_KEY"),
        (("carrito",), "DURABLE_DISPATCHER_WORKER_ID", "DURABLE_DISPATCHER_WORKER_ID"),
        (
            ("precheckout",),
            "LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY",
            "LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY",
        ),
        (
            ("precheckout",),
            "WABA_PRECHECKOUT_TEMPLATE_NAME",
            "WABA_PRECHECKOUT_TEMPLATE_NAME",
        ),
        # Con el hottok cargado y sin el freno de compra, el runtime portable no
        # arranca: no hay otra admision portable de la compra.
        (("precheckout",), "PORTABLE_HOTMART_PURCHASE_STOP_ENABLED", "hotmart_hottok"),
        # El enlace de pago es parte del entrante scoped.
        (
            ("precheckout",),
            "CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED",
            "PAYMENT_LINK_ENABLED requires scoped",
        ),
        (
            ("precheckout",),
            "DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED",
            "DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED",
        ),
    ],
)
def test_a_missing_variable_of_the_set_does_not_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flows: tuple[str, ...],
    drop: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _start(monkeypatch, tmp_path, *flows, drop=(drop,))


@pytest.mark.parametrize("flow", ["pago_fallido", "carrito"])
def test_a_hotmart_flow_without_the_hottok_starts_and_would_answer_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flow: str
) -> None:
    # Fija lo que hace hoy el arranque, no lo que conviene: solo el primer
    # contacto exige HOTMART_HOTTOK. Un carrito o un pago fallido sin el hottok
    # arrancan, y el webhook de Hotmart responde 503 hotmart_not_configured: el
    # flujo queda prendido sin recibir eventos. El runbook carga el hottok
    # antes (paso 8); si el arranque pasa a exigirlo, este test se invierte.
    settings, app = _start(monkeypatch, tmp_path, flow, drop=("HOTMART_HOTTOK",))

    assert settings.hotmart_hottok is None
    assert app.state.durable_dispatcher is not None  # type: ignore[attr-defined]

    async def post() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/webhooks/hotmart", headers={"X-HOTMART-HOTTOK": "x"}, json={}
            )

    response = asyncio.run(post())
    assert response.status_code == 503
    assert "hotmart_not_configured" in response.text


@pytest.mark.parametrize("flow", ["pago_fallido", "precheckout"])
def test_without_the_written_acceptance_the_gated_flows_do_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flow: str
) -> None:
    # El estado de ATT1 antes de la firma: la seccion del adaptador sin la
    # aceptacion. Con el set completo cargado, el flujo no arranca.
    with pytest.raises(ValueError, match="without the written risk acceptance"):
        _start(monkeypatch, tmp_path, flow, accepted=False)


def test_a_flow_variable_without_its_flow_in_the_manifest_does_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Las variables del primer contacto cargadas antes de mergear el manifiesto
    # con flujos.precheckout = true.
    instance = _write_instance(tmp_path, "inbound", "carrito")
    env = _production_env(instance, "carrito", "precheckout")
    _load(monkeypatch, env)

    with pytest.raises(ValueError, match="precheckout"):
        _build_app()


def test_the_set_does_not_leak_into_the_environment_of_the_suite() -> None:
    # monkeypatch deshace todo: ningun test de arriba deja el manifiesto cargado.
    assert os.environ.get("INSTANCE_MANIFEST_PATH", "") == ""
