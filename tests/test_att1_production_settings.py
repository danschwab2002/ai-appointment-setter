"""El bridge de ATT1 arranca con el set completo de variables de produccion.

Los demas tests arman ``Settings`` a mano, campo por campo. Aca el camino es el
del contenedor: variables de entorno -> ``build_app()`` (``Settings.from_env()`` y
``create_app``, con el procesador de Hermes que arma la fabrica de uvicorn), con
el manifiesto de ATT1 escrito en disco. Cubre lo que ninguna otra suite
prueba junto: que el set de variables con el que la instancia va a prender cada
flujo (pago fallido, carrito y primer contacto tras el formulario), y los tres a
la vez, pasa todas las validaciones de arranque y arma los workers.

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

import httpx
import pytest

import bridge.app as app_module
from bridge.app import Settings
from bridge.instance_manifest import GHL_ADAPTER_RISK_CONTRACT
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
        "HERMES_API_BASE_URL": "http://hermes:8644",
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
    env = _production_env(instance, *OUTBOUND_FLOWS)

    assert sorted(set(env) - set(_BRIDGE_VARIABLES)) == []


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
