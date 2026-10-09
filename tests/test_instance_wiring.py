"""El bridge con manifiesto de instancia v2 y conocimiento comercial (F2b).

Fixtures: tests/fixtures/instances/att1 (datos de ATT1 medidos el 2026-09-28).
"""

from __future__ import annotations

import asyncio
from dataclasses import fields as dataclass_fields, replace
from datetime import date
import json
from pathlib import Path
import re
import shutil
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.app import (
    Settings,
    _MEDICATION_GUIDANCE_ACTION_RE,
    _MEDICATION_GUIDANCE_SUBJECT_RE,
    _ghl_adapter_audience_block,
    _requires_medication_guidance_handoff,
    _stem_pattern,
    _waba_template_config,
    create_app,
)
from bridge.chatwoot import ChatwootClient
from bridge.commercial_knowledge import CommercialKnowledge, KnowledgeError
from bridge.hermes import HermesShadowProcessor
from bridge.instance_manifest import (
    GhlFormLandings,
    GhlRiskAcceptance,
    InstanceManifest,
    Template,
)
from bridge.supabase import PilotBoundaryConfig, SupabaseError

ATT1 = Path(__file__).parent / "fixtures" / "instances" / "att1"


def _approve(knowledge_path: Path) -> None:
    text = knowledge_path.read_text(encoding="utf-8")
    knowledge_path.write_text(
        text.replace(
            'estado = "borrador"',
            'estado = "aprobado"\naprobado_por = "test"\naprobado_el = 2026-09-28',
        ),
        encoding="utf-8",
    )


@pytest.fixture
def instance(tmp_path: Path) -> Path:
    target = tmp_path / "instancia"
    shutil.copytree(ATT1, target)
    return target


def _environment(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    for name, value in {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "2",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "11",
        **extra,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("COMMERCIAL_ALLY_CONFIG_PATH", raising=False)


def _settings(manifest: InstanceManifest, **overrides: object) -> Settings:
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid=None,
        capture_dir=Path("/tmp/instance-wiring-captures"),
        max_age_seconds=300,
        commercial_ally_config=manifest.to_commercial_ally_config(),
        commercial_ally_manifest_path=Path("/instancia/instancia.toml"),
        instance_manifest=manifest,
        hermes_model_name=manifest.agent_model_name,
    )
    return replace(settings, **overrides)


def _att1_manifest(**flows: bool) -> InstanceManifest:
    manifest = InstanceManifest.from_toml_file(ATT1 / "instancia.toml")
    if flows:
        manifest = replace(manifest, flows={**manifest.flows, **flows})
    return manifest


# ---------------------------------------------------------------- from_env


def test_from_env_takes_the_binding_from_the_instance_manifest(
    monkeypatch: pytest.MonkeyPatch, instance: Path
) -> None:
    _environment(monkeypatch, INSTANCE_MANIFEST_PATH=str(instance / "instancia.toml"))

    settings = Settings.from_env()

    assert settings.instance_manifest is not None
    assert settings.instance_manifest.ally_ref == "att1"
    assert settings.commercial_ally_config.hotmart_product_id == 5071808
    assert settings.commercial_ally_config.offer_code == "gopi6lh7"
    assert (settings.commercial_ally_config.chatwoot_account_id, settings.commercial_ally_config.chatwoot_inbox_id) == (2, 11)
    assert settings.commercial_ally_manifest_path == instance / "instancia.toml"
    assert settings.commercial_knowledge is None


def test_from_env_rejects_both_manifest_variables(
    monkeypatch: pytest.MonkeyPatch, instance: Path
) -> None:
    _environment(monkeypatch, INSTANCE_MANIFEST_PATH=str(instance / "instancia.toml"))
    monkeypatch.setenv("COMMERCIAL_ALLY_CONFIG_PATH", "/runtime/commercial-ally.json")

    with pytest.raises(ValueError, match="mutually exclusive"):
        Settings.from_env()


def test_knowledge_requires_an_instance_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    _environment(monkeypatch, COMMERCIAL_KNOWLEDGE_ENABLED="true")
    monkeypatch.delenv("INSTANCE_MANIFEST_PATH", raising=False)

    with pytest.raises(ValueError, match="requires INSTANCE_MANIFEST_PATH"):
        Settings.from_env()


def test_draft_knowledge_does_not_start(monkeypatch: pytest.MonkeyPatch, instance: Path) -> None:
    _environment(
        monkeypatch,
        INSTANCE_MANIFEST_PATH=str(instance / "instancia.toml"),
        COMMERCIAL_KNOWLEDGE_ENABLED="true",
    )

    with pytest.raises(KnowledgeError, match="borrador"):
        Settings.from_env()


def test_approved_knowledge_is_loaded_from_the_manifest_path(
    monkeypatch: pytest.MonkeyPatch, instance: Path
) -> None:
    _approve(instance / "conocimiento" / "knowledge-v1.toml")
    _environment(
        monkeypatch,
        INSTANCE_MANIFEST_PATH=str(instance / "instancia.toml"),
        COMMERCIAL_KNOWLEDGE_ENABLED="true",
    )

    settings = Settings.from_env()

    assert settings.commercial_knowledge is not None
    assert settings.commercial_knowledge.ally_ref == "att1"
    assert settings.commercial_knowledge.approved


# ----------------------------------------------------------- create_app gates


def test_manifest_runtime_with_everything_off_builds() -> None:
    assert create_app(_settings(_att1_manifest())) is not None


def test_agent_model_must_match_the_manifest() -> None:
    settings = _settings(_att1_manifest(), hermes_model_name="agente-comercial")

    with pytest.raises(ValueError, match="HERMES_MODEL_NAME"):
        create_app(settings)


@pytest.mark.parametrize(
    ("flag", "flow"),
    [
        ("automated_replies_enabled", "inbound"),
        ("chatwoot_cut_b_agent_enabled", "inbound"),
        ("portable_hotmart_recovery_enabled", "carrito"),
        ("portable_hotmart_payment_failure_enabled", "pago_fallido"),
        ("portable_precheckout_first_contact_enabled", "precheckout"),
        ("conversation_reactivation_enabled", "reactivacion"),
        ("chatwoot_post_inbound_discount_planning_enabled", "descuento"),
    ],
)
def test_a_flag_cannot_exceed_its_declared_flow(flag: str, flow: str) -> None:
    settings = _settings(_att1_manifest(), **{flag: True})

    with pytest.raises(ValueError, match=f"{flag}->{flow}"):
        create_app(settings)


def test_lead_precheckout_needs_the_intent_event() -> None:
    settings = _settings(_att1_manifest(), lead_precheckout_enabled=True)

    with pytest.raises(ValueError, match="lead_precheckout_enabled->intencion"):
        create_app(settings)


def test_payment_link_is_part_of_the_inbound_flow() -> None:
    # PAYMENT_LINK_ENABLED ya exige Cut B y respuestas automaticas, y esas exigen el
    # flujo inbound: se prueba que con todo prendido menos el flujo, corta el manifiesto.
    settings = _settings(
        _att1_manifest(),
        payment_link_enabled=True,
        chatwoot_cut_b_admission_enabled=True,
        chatwoot_cut_b_agent_enabled=True,
        automated_replies_enabled=True,
        chatwoot_scoped_inbound_senders_enabled=True,
    )

    with pytest.raises(ValueError, match="payment_link_enabled->inbound"):
        create_app(settings)


def test_final_meta_effect_needs_an_outbound_flow() -> None:
    with pytest.raises(ValueError, match="meta_final_effect_enabled->outbound"):
        create_app(_settings(_att1_manifest(), meta_final_effect_enabled=True))


def test_flag_with_its_flow_declared_passes_the_manifest_gate() -> None:
    settings = _settings(_att1_manifest(carrito=True), portable_hotmart_recovery_enabled=True)

    try:
        create_app(settings)
    except ValueError as exc:
        assert "exceed the instance manifest" not in str(exc)


def test_automated_replies_need_approved_knowledge() -> None:
    settings = _settings(_att1_manifest(inbound=True), automated_replies_enabled=True)

    with pytest.raises(ValueError, match="COMMERCIAL_KNOWLEDGE_ENABLED"):
        create_app(settings)


def test_knowledge_of_another_ally_is_refused(instance: Path) -> None:
    path = instance / "conocimiento" / "knowledge-v1.toml"
    _approve(path)
    knowledge = replace(CommercialKnowledge.from_toml_file(path), ally_ref="johanna")

    with pytest.raises(ValueError, match="another ally"):
        create_app(_settings(_att1_manifest(), commercial_knowledge=knowledge))


def test_knowledge_without_manifest_is_refused(instance: Path) -> None:
    path = instance / "conocimiento" / "knowledge-v1.toml"
    _approve(path)
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid=None,
        capture_dir=Path("/tmp/instance-wiring-captures"),
        max_age_seconds=300,
        commercial_knowledge=CommercialKnowledge.from_toml_file(path),
    )

    with pytest.raises(ValueError, match="requires an instance manifest"):
        create_app(settings)


# ---------------------------------------------------------- adaptador de GHL
# docs/contracts/ghl-precheckout-adapter-v1.md, "Configuracion". El fixture de ATT1
# es copia de la instancia y todavia no tiene "intencion" ni [adaptadores.ghl]: se
# arman por mutacion. EgDq es el formulario de ads-a medido el 2026-09-29
# (tests/fixtures/ghl/).

_GHL_FORM = "EgDqRl2xWc59YjVW1q8W"
_GHL_LANDING_D_FORM = "Om5FpIg5Sr5ce7nSkuPy"
_GHL_TOKEN = "ghl-adapter-test-token-0123456789abcdef"


# Una aceptacion DE PRUEBA del riesgo del adaptador. Ningun manifiesto real la
# recibe de este codigo: la escribe a mano quien firma, en la instancia.
_TEST_RISK_ACCEPTANCE = GhlRiskAcceptance(
    accepted_by="aceptacion de prueba (tests)",
    accepted_on=date(2026, 10, 1),
    contract="ghl-precheckout-adapter-v1",
)


def _ghl_manifest(
    *,
    intencion: bool = True,
    forms: tuple[str, ...] = (_GHL_FORM,),
    accepted: bool = False,
    **flows: bool,
) -> InstanceManifest:
    manifest = _att1_manifest(**flows)
    events = manifest.events | {"intencion"} if intencion else manifest.events
    return replace(
        manifest,
        events=frozenset(events),
        ghl_form_ids=forms,
        ghl_risk_acceptance=_TEST_RISK_ACCEPTANCE if accepted else None,
    )


def _ghl_settings(
    manifest: InstanceManifest | None = None, **overrides: object
) -> Settings:
    return _settings(
        manifest or _ghl_manifest(),
        **{
            "ghl_precheckout_adapter_enabled": True,
            "ghl_precheckout_adapter_token": _GHL_TOKEN,
            **overrides,
        },
    )


def _johanna_settings(**overrides: object) -> Settings:
    # Johanna hoy: sin manifiesto, con /webhooks/lead prendido y su secreto.
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=Path("/tmp/instance-wiring-captures"),
        max_age_seconds=300,
        lead_precheckout_enabled=True,
        lead_precheckout_secret="johanna-lead-secret",
    )
    return replace(settings, **overrides)


def test_ghl_adapter_with_the_intent_its_forms_and_a_token_builds() -> None:
    # Con manifiesto el bridge rechaza toda capacidad que no este en
    # PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES: que arranque prueba que el flag lo esta.
    assert create_app(_ghl_settings()) is not None


def test_johanna_without_token_and_the_flag_off_starts_as_today() -> None:
    # E11: con el flag apagado no se evalua ninguna guarda del adaptador, ni la
    # comparacion de un token ausente contra un secreto ausente o vacio.
    for settings in (
        _johanna_settings(),
        _johanna_settings(lead_precheckout_enabled=False, lead_precheckout_secret=None),
        replace(_johanna_settings(), webhook_secret="", lead_precheckout_secret=""),
    ):
        assert settings.ghl_precheckout_adapter_enabled is False
        assert settings.ghl_precheckout_adapter_token is None
        with TestClient(create_app(settings)) as client:
            response = client.get("/ready")

        assert response.status_code == 200
        assert "ghl_precheckout_adapter" not in response.json()
        assert "ghl_adapter_risk" not in response.json()


def test_the_ghl_adapter_requires_an_instance_manifest() -> None:
    johanna = _johanna_settings(
        ghl_precheckout_adapter_enabled=True, ghl_precheckout_adapter_token=_GHL_TOKEN
    )
    # El binding v1 (COMMERCIAL_ALLY_CONFIG_PATH) es un runtime portable sin
    # manifiesto v2: tampoco alcanza.
    binding_v1 = replace(_ghl_settings(), instance_manifest=None)

    for settings in (johanna, binding_v1):
        with pytest.raises(
            ValueError, match="GHL_PRECHECKOUT_ADAPTER_ENABLED requires an instance manifest"
        ):
            create_app(settings)


def test_the_ghl_adapter_needs_the_intent_event() -> None:
    settings = _ghl_settings(_ghl_manifest(intencion=False))

    with pytest.raises(ValueError, match="ghl_precheckout_adapter_enabled->intencion"):
        create_app(settings)


def test_the_ghl_adapter_needs_its_forms_in_the_manifest() -> None:
    settings = _ghl_settings(_ghl_manifest(forms=()))

    with pytest.raises(
        ValueError, match="ghl_precheckout_adapter_enabled->adaptadores.ghl"
    ):
        create_app(settings)


# E4 (cabo LAN-054): el token del adaptador es la unica barrera y una intencion que
# entro por el adaptador no se distingue en la base de la de una landing. La salida
# es la aceptacion escrita del riesgo en [adaptadores.ghl]; sin ella no arrancan los
# dos flujos que usan la intencion como permiso de contacto. La condicion la hace
# cumplir el arranque, no la relectura del contrato.


@pytest.mark.parametrize(
    ("flows", "named"),
    [
        ({"precheckout": True}, "flujos.precheckout on"),
        ({"pago_fallido": True}, "flujos.pago_fallido on"),
        (
            {"precheckout": True, "pago_fallido": True},
            "flujos.precheckout, flujos.pago_fallido on",
        ),
    ],
    ids=["precheckout", "pago_fallido", "los-dos"],
)
def test_the_adapter_section_without_the_acceptance_does_not_start_a_gated_flow(
    flows: dict[str, bool], named: str
) -> None:
    manifest = _ghl_manifest(**flows)
    expected = re.escape(f"[adaptadores.ghl] cannot run with {named} without the written risk acceptance")

    with pytest.raises(ValueError, match=expected):
        create_app(_ghl_settings(manifest))
    # La guarda cuelga de la seccion y no del flag (D6): apagar el adaptador no
    # saca de la base las intenciones que ya admitio.
    with pytest.raises(ValueError, match=expected):
        create_app(_settings(manifest))


def test_the_gate_error_names_the_way_out() -> None:
    with pytest.raises(ValueError) as error:
        create_app(_ghl_settings(_ghl_manifest(precheckout=True)))

    message = str(error.value)
    for key in ("riesgo_aceptado_por", "riesgo_aceptado_el", "riesgo_contrato"):
        assert key in message
    assert "out-of-band check" in message


@pytest.mark.parametrize(
    "flows",
    [{"precheckout": True}, {"pago_fallido": True}, {"precheckout": True, "pago_fallido": True}],
    ids=["precheckout", "pago_fallido", "los-dos"],
)
def test_with_the_written_acceptance_the_gated_flows_start(flows: dict[str, bool]) -> None:
    manifest = _ghl_manifest(accepted=True, **flows)

    assert create_app(_ghl_settings(manifest)) is not None
    assert create_app(_settings(manifest)) is not None


@pytest.mark.parametrize("flow", ["inbound", "carrito"])
def test_the_flows_that_do_not_grant_from_the_intent_start_without_the_acceptance(
    flow: str,
) -> None:
    # El carrito no concede permiso desde la intencion y el entrante responde a
    # quien escribio. La audiencia del carrito (consented_intent) vive en la base:
    # la corta la guarda de audiencia, mas abajo.
    manifest = _ghl_manifest(**{flow: True})

    assert create_app(_ghl_settings(manifest)) is not None


@pytest.mark.parametrize("flow", ["precheckout", "pago_fallido"])
def test_without_the_adapter_section_a_gated_flow_starts_with_a_warning(
    flow: str, caplog: pytest.LogCaptureFixture
) -> None:
    # D6 y la correccion 14 en su version minima: sin [adaptadores.ghl] nada
    # bloquea. Si la instancia uso el adaptador, sus intenciones siguen en la base:
    # el arranque lo avisa y /ready lo publica.
    manifest = _ghl_manifest(forms=(), **{flow: True})
    assert manifest.ghl_form_ids == ()

    with caplog.at_level("WARNING", logger="bridge.app"):
        ready = _ready(_settings(manifest))

    assert ready["ghl_adapter_risk"] == "no_adapter_section"
    [record] = [r for r in caplog.records if r.getMessage().startswith("ghl_adapter_risk ")]
    assert record.levelname == "WARNING"
    assert record.getMessage() == (
        f"ghl_adapter_risk acceptance=no_adapter_section flows={flow} "
        "detail=intents_admitted_by_a_removed_adapter_are_not_gated"
    )


@pytest.mark.parametrize("token", [None, "x" * 31])
def test_the_ghl_adapter_needs_a_token_of_32_characters(token: str | None) -> None:
    with pytest.raises(ValueError, match="GHL_PRECHECKOUT_ADAPTER_TOKEN must contain"):
        create_app(_ghl_settings(ghl_precheckout_adapter_token=token))


def test_a_ghl_adapter_token_of_exactly_32_characters_builds() -> None:
    assert create_app(_ghl_settings(ghl_precheckout_adapter_token="x" * 32)) is not None


# Todo secreto de texto de Settings: el token del adaptador lo lee cualquier usuario
# de la subcuenta de GHL, y si repite otro secreto le da esa otra autoridad (el del
# primer contacto manda mensajes). La lista sale de Settings, asi un secreto nuevo
# queda cubierto sin tocar el test.
_SECRET_FIELD = re.compile(r"(secret|token|key|hottok)$")
_SETTINGS_SECRETS = sorted(
    field.name
    for field in dataclass_fields(Settings)
    if field.type in ("str", "str | None")
    and _SECRET_FIELD.search(field.name)
    and field.name != "ghl_precheckout_adapter_token"
)


def test_the_secret_list_covers_the_ones_the_review_named() -> None:
    assert {
        "lead_precheckout_secret",
        "webhook_secret",
        "precheckout_form_token",
        "precheckout_first_touch_token",
        "johanna_abandonment_one_shot_token",
        "operator_correlation_read_token",
        "operator_correlation_write_token",
        "slack_connector_bearer_token",
        "hotmart_hottok",
        "supabase_service_role_key",
        "hermes_api_key",
        "openrouter_api_key",
        "chatwoot_control_api_access_token",
        "chatwoot_agent_bot_access_token",
    } <= set(_SETTINGS_SECRETS)


# El runtime con manifiesto no acepta estos secretos cargados, sea cual sea su valor:
# el arranque los corta antes de comparar el token. Si alguno pasa a ser portable,
# este test falla y hay que mirarlo.
_REFUSED_BY_THE_MANIFEST_RUNTIME = {"hotmart_hottok"}


@pytest.mark.parametrize("secret", _SETTINGS_SECRETS)
def test_the_ghl_adapter_token_must_differ_from_the_other_secrets(secret: str) -> None:
    settings = _ghl_settings(**{secret: _GHL_TOKEN})
    expected = (
        f"runtime capabilities are not portable: {secret}$"
        if secret in _REFUSED_BY_THE_MANIFEST_RUNTIME
        else f"GHL_PRECHECKOUT_ADAPTER_TOKEN must differ.*it equals {secret}$"
    )

    with pytest.raises(ValueError, match=expected):
        create_app(settings)


def test_from_env_leaves_the_ghl_adapter_off_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # El entorno de Johanna: cuenta 1 e inbox 9 de Chatwoot, sin manifiesto.
    _environment(monkeypatch, CHATWOOT_ACCOUNT_ID="1", CHATWOOT_INBOX_ID="9")
    for name in (
        "INSTANCE_MANIFEST_PATH",
        "GHL_PRECHECKOUT_ADAPTER_ENABLED",
        "GHL_PRECHECKOUT_ADAPTER_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert settings.ghl_precheckout_adapter_enabled is False
    assert settings.ghl_precheckout_adapter_token is None


@pytest.mark.parametrize("token", ["", "x" * 31])
def test_from_env_refuses_the_ghl_adapter_without_a_long_token(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    _environment(
        monkeypatch,
        GHL_PRECHECKOUT_ADAPTER_ENABLED="true",
        GHL_PRECHECKOUT_ADAPTER_TOKEN=token,
    )

    with pytest.raises(ValueError, match="GHL_PRECHECKOUT_ADAPTER_TOKEN must contain"):
        Settings.from_env()


def test_from_env_to_ready_with_the_ghl_adapter_of_the_instance(
    monkeypatch: pytest.MonkeyPatch, instance: Path
) -> None:
    path = instance / "instancia.toml"
    text = path.read_text(encoding="utf-8")
    events = 'eventos = ["carrito", "pago_fallido", "compra", "entrante"]'
    assert events in text
    path.write_text(
        text.replace(events, events[:-1] + ', "intencion"]')
        + f'\n[adaptadores.ghl]\nformularios = ["{_GHL_FORM}"]\n',
        encoding="utf-8",
    )
    manifest = InstanceManifest.from_toml_file(path)
    _environment(
        monkeypatch,
        INSTANCE_MANIFEST_PATH=str(path),
        HERMES_MODEL_NAME=manifest.agent_model_name,
        GHL_PRECHECKOUT_ADAPTER_ENABLED="true",
        GHL_PRECHECKOUT_ADAPTER_TOKEN=_GHL_TOKEN,
    )

    settings = Settings.from_env()

    assert settings.ghl_precheckout_adapter_enabled is True
    assert settings.ghl_precheckout_adapter_token == _GHL_TOKEN
    assert settings.instance_manifest is not None
    assert settings.instance_manifest.ghl_form_ids == (_GHL_FORM,)
    with TestClient(create_app(settings, supabase_client=_BindingAuthority())) as client:  # type: ignore[arg-type]
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["ghl_precheckout_adapter"] == "enabled:1-forms"


# ------------------------------------------------ parametros de las plantillas


_WABA_OUTBOUND = {
    "dispatcher_outbound_enabled": True,
    "pilot_boundary_enabled": True,
    "pilot_scope_key": "att1-recuperacion",
    "pilot_scope_version": 1,
    "pilot_tenant_key": "lancemos",
    "pilot_channel_provider": "waba",
    "pilot_channel_account_ref": "chatwoot-inbox:11",
    "waba_first_touch_template_name": "att1_carrito_abandonado_01",
    "waba_payment_failure_template_name": "att1_compra_fallida_01",
    "waba_template_language": "es_MX",
    "waba_template_category": "MARKETING",
}


def _with_parameters(manifest: InstanceManifest, **parameters: tuple[str, ...]) -> InstanceManifest:
    templates = dict(manifest.templates)
    for slot, declared in parameters.items():
        templates[slot] = replace(templates[slot], parameters=declared)
    return replace(manifest, templates=templates)


def test_without_manifest_the_waba_template_declares_no_variables() -> None:
    # Johanna corre sin manifiesto: la plantilla sale igual que hoy.
    settings = replace(
        Settings(
            webhook_secret="test-secret",
            allowed_jid=None,
            capture_dir=Path("/tmp/instance-wiring-captures"),
            max_age_seconds=300,
        ),
        **_WABA_OUTBOUND,
    )

    template = _waba_template_config(settings)

    assert template is not None
    assert template.first_touch_parameter == "buyer_name_and_product"
    assert template.first_touch_body_parameters is None
    assert template.payment_failure_body_parameters is None


def test_flows_on_take_the_variables_declared_in_the_manifest() -> None:
    manifest = _with_parameters(
        _att1_manifest(carrito=True, pago_fallido=True),
        carrito=("nombre",),
        pago_fallido=("producto", "nombre"),
    )

    template = _waba_template_config(_settings(manifest, **_WABA_OUTBOUND))

    assert template is not None
    assert template.first_touch_body_parameters == ("nombre",)
    assert template.payment_failure_body_parameters == ("producto", "nombre")
    assert template.params(
        content="copy", followup=False, buyer_name="Ana", product_name="ATT1",
        trigger_kind="payment_failure",
    )["processed_params"] == {"body": {"1": "ATT1", "2": "Ana"}}


def test_manifest_without_parametros_sends_the_two_variables_of_today() -> None:
    template = _waba_template_config(
        _settings(_att1_manifest(carrito=True, pago_fallido=True), **_WABA_OUTBOUND)
    )

    assert template is not None
    assert template.first_touch_body_parameters == ("nombre", "producto")
    assert template.payment_failure_body_parameters == ("nombre", "producto")


def test_a_flow_that_is_off_does_not_lend_its_variables() -> None:
    manifest = _with_parameters(_att1_manifest(), carrito=("nombre",))

    template = _waba_template_config(_settings(manifest, **_WABA_OUTBOUND))

    assert template is not None
    assert template.first_touch_body_parameters is None
    assert template.payment_failure_body_parameters is None


def test_payment_failure_without_its_own_template_keeps_the_cart_variables() -> None:
    manifest = _with_parameters(
        _att1_manifest(carrito=True, pago_fallido=True), carrito=("nombre",)
    )
    settings = _settings(
        manifest, **{**_WABA_OUTBOUND, "waba_payment_failure_template_name": None}
    )

    template = _waba_template_config(settings)

    assert template is not None
    assert template.payment_failure_name is None
    assert template.first_touch_body_parameters == ("nombre",)
    assert template.payment_failure_body_parameters is None


@pytest.mark.parametrize(
    ("flows", "override", "message"),
    [
        (
            {"carrito": True},
            {"waba_first_touch_template_name": "johanna_carrito_abandonado_01"},
            "WABA_FIRST_TOUCH_TEMPLATE_NAME must match plantillas.carrito.nombre",
        ),
        (
            {"pago_fallido": True},
            {"waba_payment_failure_template_name": "johanna_compra_fallida_01"},
            "WABA_PAYMENT_FAILURE_TEMPLATE_NAME must match plantillas.pago_fallido.nombre",
        ),
        (
            {"carrito": True},
            {"waba_template_language": "es_EC"},
            "WABA_TEMPLATE_LANGUAGE must match plantillas.carrito.idioma",
        ),
    ],
)
def test_waba_template_variables_must_name_the_manifest_template(
    flows: dict[str, bool], override: dict[str, str], message: str
) -> None:
    settings = _settings(_att1_manifest(**flows), **{**_WABA_OUTBOUND, **override})

    with pytest.raises(ValueError, match=message):
        create_app(settings)


def test_cart_and_payment_failure_templates_must_share_one_language() -> None:
    manifest = _att1_manifest(carrito=True, pago_fallido=True)
    templates = dict(manifest.templates)
    templates["pago_fallido"] = Template(name="att1_compra_fallida_01", language="es_AR")
    settings = _settings(replace(manifest, templates=templates), **_WABA_OUTBOUND)

    with pytest.raises(ValueError, match="plantillas.pago_fallido.idioma"):
        create_app(settings)


def test_matching_waba_template_variables_build_with_the_manifest() -> None:
    manifest = _with_parameters(_att1_manifest(carrito=True), carrito=("nombre",))

    assert create_app(_settings(manifest, **_WABA_OUTBOUND)) is not None


# --------------------------------------- categoria de la plantilla del pago fallido


def test_from_env_reads_the_payment_failure_category(
    monkeypatch: pytest.MonkeyPatch, instance: Path
) -> None:
    _environment(monkeypatch, INSTANCE_MANIFEST_PATH=str(instance / "instancia.toml"))
    monkeypatch.delenv("WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY", raising=False)

    assert Settings.from_env().waba_payment_failure_template_category is None

    monkeypatch.setenv("WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY", " utility ")

    assert Settings.from_env().waba_payment_failure_template_category == "UTILITY"


def test_the_payment_failure_template_takes_its_own_category() -> None:
    settings = _settings(
        _att1_manifest(carrito=True, pago_fallido=True),
        **{**_WABA_OUTBOUND, "waba_payment_failure_template_category": "UTILITY"},
    )

    template = _waba_template_config(settings)

    assert template is not None
    assert template.category_for(trigger_kind="payment_failure") == "UTILITY"
    assert template.category_for(trigger_kind="cart_abandonment") == "MARKETING"
    assert create_app(settings) is not None


def test_without_its_own_category_the_payment_failure_keeps_the_single_one() -> None:
    template = _waba_template_config(
        _settings(_att1_manifest(carrito=True, pago_fallido=True), **_WABA_OUTBOUND)
    )

    assert template is not None
    assert template.payment_failure_category is None
    assert template.category_for(trigger_kind="payment_failure") == "MARKETING"


def test_the_payment_failure_category_requires_an_instance_manifest() -> None:
    # Johanna comparte esta configuracion con sus one-shots: no la puede definir.
    settings = replace(
        Settings(
            webhook_secret="test-secret",
            allowed_jid=None,
            capture_dir=Path("/tmp/instance-wiring-captures"),
            max_age_seconds=300,
        ),
        **{**_WABA_OUTBOUND, "waba_payment_failure_template_category": "UTILITY"},
    )

    with pytest.raises(ValueError, match="requires an instance manifest"):
        _waba_template_config(settings)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (
            {"waba_payment_failure_template_category": "AUTHENTICATION"},
            "WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY must be MARKETING or UTILITY",
        ),
        (
            {
                "waba_payment_failure_template_category": "UTILITY",
                "waba_payment_failure_template_name": None,
            },
            "WABA_PAYMENT_FAILURE_TEMPLATE_CATEGORY requires WABA_PAYMENT_FAILURE_TEMPLATE_NAME",
        ),
    ],
)
def test_an_invalid_payment_failure_category_does_not_start(
    override: dict[str, object], message: str
) -> None:
    settings = _settings(_att1_manifest(carrito=True), **{**_WABA_OUTBOUND, **override})

    with pytest.raises(ValueError, match=message):
        create_app(settings)


# ------------------------------------------- plantilla aprobada sin Hermes (A4)


_DIRECT = {
    **_WABA_OUTBOUND,
    "portable_hotmart_recovery_enabled": True,
    "dispatcher_approved_template_direct_enabled": True,
}


def test_from_env_reads_the_direct_mode_default_off(
    monkeypatch: pytest.MonkeyPatch, instance: Path
) -> None:
    _environment(monkeypatch, INSTANCE_MANIFEST_PATH=str(instance / "instancia.toml"))
    monkeypatch.delenv("DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED", raising=False)

    assert Settings.from_env().dispatcher_approved_template_direct_enabled is False

    monkeypatch.setenv("DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED", "true")

    assert Settings.from_env().dispatcher_approved_template_direct_enabled is True


def test_direct_mode_with_its_flow_builds() -> None:
    assert create_app(_settings(_att1_manifest(carrito=True), **_DIRECT)) is not None


def test_direct_mode_requires_an_instance_manifest() -> None:
    # Johanna corre sin manifiesto: el modo directo no se le puede prender.
    settings = replace(
        Settings(
            webhook_secret="test-secret",
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=Path("/tmp/instance-wiring-captures"),
            max_age_seconds=300,
        ),
        **{**_WABA_OUTBOUND, "dispatcher_approved_template_direct_enabled": True},
    )

    with pytest.raises(ValueError, match="requires an instance manifest"):
        create_app(settings)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"dispatcher_outbound_enabled": False}, "requires DURABLE_OUTBOUND_ENABLED"),
        ({"portable_hotmart_recovery_enabled": False}, "requires a portable recovery flow"),
    ],
)
def test_direct_mode_requires_the_durable_waba_outbound_of_a_portable_flow(
    override: dict[str, object], message: str
) -> None:
    settings = _settings(_att1_manifest(carrito=True), **{**_DIRECT, **override})

    with pytest.raises(ValueError, match=message):
        create_app(settings)


def test_final_meta_effect_of_the_durable_outbound_requires_the_direct_mode() -> None:
    # Sin el modo directo el texto sale de un borrador de Hermes, que nunca llega
    # a Meta y que el SOUL comun prohibe: con manifiesto no se abre el gate.
    settings = _settings(
        _att1_manifest(carrito=True),
        **{
            **_DIRECT,
            "dispatcher_approved_template_direct_enabled": False,
            "meta_final_effect_enabled": True,
        },
    )

    with pytest.raises(ValueError, match="requires DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED"):
        create_app(settings)


def test_final_meta_effect_with_the_direct_mode_builds() -> None:
    # Tambien fija que el candado viejo de ATT1 (tenant_ref == "att1", del
    # binding v1) no dispara con el manifiesto v2 (tenant_ref "lancemos"): para
    # ATT1 v2 el corte del efecto final es META_FINAL_EFFECT_ENABLED.
    assert _att1_manifest().to_commercial_ally_config().tenant_ref == "lancemos"
    settings = _settings(
        _att1_manifest(carrito=True), **{**_DIRECT, "meta_final_effect_enabled": True}
    )

    assert create_app(settings) is not None


def test_the_greeting_is_a_portable_capability() -> None:
    # Hasta A4 un runtime portable rechazaba LEAD_FIRST_NAME_GREETING_ENABLED al
    # arrancar.
    settings = _settings(
        _att1_manifest(carrito=True), **{**_DIRECT, "lead_first_name_greeting_enabled": True}
    )

    assert create_app(settings) is not None


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="everything off"),
        pytest.param(
            {**_DIRECT, "dispatcher_approved_template_direct_enabled": False},
            id="durable outbound in Hermes mode",
        ),
    ],
)
def test_the_greeting_without_the_direct_mode_does_not_start(
    overrides: dict[str, object],
) -> None:
    # En un runtime portable el saludo solo llega al dispatcher directo: sin el
    # modo directo el flag se aceptaba y no saludaba a nadie, sin avisar.
    settings = _settings(
        _att1_manifest(carrito=True),
        **{**overrides, "lead_first_name_greeting_enabled": True},
    )

    with pytest.raises(
        ValueError,
        match="LEAD_FIRST_NAME_GREETING_ENABLED in a portable runtime requires "
        "DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED",
    ):
        create_app(settings)


def test_johanna_keeps_the_greeting_without_the_direct_mode() -> None:
    # Sin manifiesto el saludo es de los one-shots de Johanna: no cambia.
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=Path("/tmp/instance-wiring-captures"),
        max_age_seconds=300,
        lead_first_name_greeting_enabled=True,
    )

    assert create_app(settings) is not None


def test_the_direct_dispatcher_does_not_consume_the_handoff_admission() -> None:
    # La admision de derivacion necesita quien la consuma: el Corte B o un
    # dispatcher que le pregunte a Hermes. El modo directo no le pregunta.
    settings = _settings(
        _att1_manifest(carrito=True),
        **{
            **_DIRECT,
            "dispatcher_enabled": True,
            "human_handoff_admission_enabled": True,
            "human_handoff_projection_enabled": True,
            "handoff_projection_policy_key": "att1-derivacion",
            "handoff_projection_policy_version": 1,
        },
    )

    with pytest.raises(ValueError, match="HUMAN_HANDOFF_ADMISSION_ENABLED requires"):
        create_app(settings)


# ------------------------------------------- primer contacto tras el formulario
# El flag PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED hace que la admision del
# formulario planifique un primer contacto y que el dispatcher lo mande. Solo
# arranca con todo lo que ese envio necesita y con todo lo que lo frena. El
# manifiesto es el fixture de ATT1 con "intencion" y los flujos precheckout e
# inbound prendidos por mutacion (el fixture es copia de la instancia del
# 28/09, con todo apagado). El formulario entra por /webhooks/lead: el caso con
# el adaptador de GHL (que exige la aceptacion escrita del riesgo) esta en
# test_ghl_precheckout_adapter_http.py.

_FIRST_CONTACT_SCOPE = "att1-primer-contacto"
_FIRST_CONTACT_TEMPLATE = "att1_interes_precheckout_01"
_FIRST_CONTACT_FLAG = "PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED"


def _first_contact_manifest() -> InstanceManifest:
    manifest = _att1_manifest(precheckout=True, inbound=True)
    return replace(manifest, events=frozenset(manifest.events | {"intencion"}))


def _first_contact_settings(instance: Path, **overrides: object) -> Settings:
    """El set completo con el que arranca el primer contacto del formulario."""
    knowledge_path = instance / "conocimiento" / "knowledge-v1.toml"
    if 'estado = "borrador"' in knowledge_path.read_text(encoding="utf-8"):
        _approve(knowledge_path)
    manifest = _first_contact_manifest()
    config = manifest.to_commercial_ally_config()
    values: dict[str, object] = {
        "commercial_knowledge": CommercialKnowledge.from_toml_file(knowledge_path),
        "agent_bot_id": 1,
        "chatwoot_account_id": config.chatwoot_account_id,
        "chatwoot_inbox_id": config.chatwoot_inbox_id,
        # La entrada del formulario.
        "lead_precheckout_enabled": True,
        "lead_precheckout_secret": "first-contact-lead-secret",
        "lead_precheckout_site": config.lead_site,
        "lead_precheckout_landing_id": config.lead_landing_id,
        "lead_precheckout_offer_code": config.offer_code,
        # El envio: frontera del piloto, dispatcher y plantilla aprobada directa.
        **_WABA_OUTBOUND,
        "dispatcher_enabled": True,
        "dispatcher_worker_id": "att1-dispatcher",
        "dispatcher_approved_template_direct_enabled": True,
        "portable_precheckout_first_contact_enabled": True,
        "pilot_precheckout_scope_key": _FIRST_CONTACT_SCOPE,
        "pilot_precheckout_scope_version": 1,
        "waba_precheckout_template_name": _FIRST_CONTACT_TEMPLATE,
        # Lo que lo frena: la compra de Hotmart y el entrante scoped, que
        # arrastra el opt-out durable, la pausa y la derivacion.
        "portable_hotmart_purchase_stop_enabled": True,
        "hotmart_hottok": "first-contact-hottok",
        "chatwoot_scoped_inbound_senders_enabled": True,
        "chatwoot_cut_b_admission_enabled": True,
        "chatwoot_cut_b_scope_key": config.inbound_scope_key,
        "chatwoot_cut_b_scope_version": config.inbound_scope_version,
        "chatwoot_cut_b_agent_enabled": True,
        "automated_replies_enabled": True,
        "chatwoot_durable_opt_out_enabled": True,
        "chatwoot_opt_out_macro_id": 5,
        "opt_out_projection_worker_id": "att1-opt-out-projection",
        "chatwoot_human_pause_enabled": True,
        "human_handoff_admission_enabled": True,
        "human_handoff_projection_enabled": True,
        "handoff_projection_policy_key": "att1-derivacion",
        "handoff_projection_policy_version": 1,
        "human_handoff_projection_worker_id": "att1-handoff-projection",
    }
    values.update(overrides)
    return replace(
        _settings(manifest),
        capture_dir=instance / "captures",
        meta_final_effect_evidence_dir=instance / "meta-effects",
        **values,
    )


class _FirstContactAuthority:
    """La base que /ready consulta, y la admision del formulario."""

    def __init__(self, **first_contact_status: object) -> None:
        self.first_contact_status = {
            "configured": True,
            "runtime_state": "inactive",
            "runtime_generation": 0,
            "reason_code": "pilot_runtime_inactive",
            **first_contact_status,
        }
        self.first_contact_boundaries: list[object] = []
        self.plan_calls: list[dict[str, object]] = []
        self.admission_calls: list[dict[str, object]] = []
        self.plan = {"plan_outcome": "planned", "plan_reason": "first_contact_scheduled"}

    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return config

    async def get_human_handoff_projection_status(self) -> object:
        return SimpleNamespace(
            pending_count=0, retryable_count=0, delivery_unknown_count=0,
            conflict_count=0, dead_letter_count=0,
        )

    async def get_pilot_runtime_status(self, *, pilot_boundary: object) -> object:
        return SimpleNamespace(
            configured=True, runtime_state="inactive", runtime_generation=0,
            reason_code="pilot_runtime_inactive",
        )

    async def get_portable_precheckout_pilot_runtime_status(
        self, *, pilot_boundary: object
    ) -> object:
        self.first_contact_boundaries.append(pilot_boundary)
        return SimpleNamespace(**self.first_contact_status)

    async def admit_portable_observed_lead_precheckout(self, **kwargs: object) -> object:
        self.admission_calls.append(kwargs)
        return SimpleNamespace(
            outcome="inserted",
            submission_id="bfc778e7-5c9f-45e6-a910-651f92312157",
            purchase_intent_id="1f581f3a-c469-45da-8208-9483d1b26f0b",
        )

    async def admit_and_plan_portable_lead_precheckout(self, **kwargs: object) -> object:
        self.plan_calls.append(kwargs)
        return SimpleNamespace(
            outcome="inserted",
            submission_id="bfc778e7-5c9f-45e6-a910-651f92312157",
            purchase_intent_id="1f581f3a-c469-45da-8208-9483d1b26f0b",
            **self.plan,
        )


class _ShadowProcessor:
    async def run(self, **_: object) -> object:
        raise AssertionError("Hermes is not called by these tests")


def _first_contact_app(
    settings: Settings, authority: _FirstContactAuthority | None = None
) -> object:
    # El dispatcher exige un ChatwootClient de verdad; ninguno de estos tests
    # llega a Chatwoot.
    return create_app(
        settings,
        chatwoot_client=ChatwootClient(
            base_url="https://chatwoot.example.test",
            account_id=settings.chatwoot_account_id or 0,
            access_token="test-control-token",
            agent_bot_access_token="test-bot-token",
            agent_bot_id=1,
            transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        ),
        shadow_processor=_ShadowProcessor(),  # type: ignore[arg-type]
        supabase_client=authority or _FirstContactAuthority(),  # type: ignore[arg-type]
    )


def _get_ready(app: object) -> httpx.Response:
    # Sin lifespan: los workers no arrancan, solo se consulta /ready.
    async def get() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/ready")

    return asyncio.run(get())


def test_from_env_reads_the_first_contact_variables_default_off(
    monkeypatch: pytest.MonkeyPatch, instance: Path
) -> None:
    _environment(monkeypatch, INSTANCE_MANIFEST_PATH=str(instance / "instancia.toml"))
    for name in (
        _FIRST_CONTACT_FLAG,
        "LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY",
        "LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION",
        "WABA_PRECHECKOUT_TEMPLATE_NAME",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert settings.portable_precheckout_first_contact_enabled is False
    assert settings.pilot_precheckout_scope_key is None
    assert settings.pilot_precheckout_scope_version is None
    assert settings.waba_precheckout_template_name is None

    monkeypatch.setenv(_FIRST_CONTACT_FLAG, "true")
    monkeypatch.setenv("LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY", f" {_FIRST_CONTACT_SCOPE} ")
    monkeypatch.setenv("LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION", "2")
    monkeypatch.setenv("WABA_PRECHECKOUT_TEMPLATE_NAME", f" {_FIRST_CONTACT_TEMPLATE} ")

    settings = Settings.from_env()

    assert settings.portable_precheckout_first_contact_enabled is True
    assert settings.pilot_precheckout_scope_key == _FIRST_CONTACT_SCOPE
    assert settings.pilot_precheckout_scope_version == 2
    assert settings.waba_precheckout_template_name == _FIRST_CONTACT_TEMPLATE


def test_the_first_contact_with_its_complete_set_builds(instance: Path) -> None:
    # Tambien prueba que el flag es una capacidad portable y que solo, sin
    # carrito ni pago fallido, alcanza para el destinatario dinamico que exige
    # el modo directo.
    settings = _first_contact_settings(instance)

    assert settings.portable_hotmart_recovery_enabled is False
    assert settings.portable_hotmart_payment_failure_enabled is False
    assert _first_contact_app(settings) is not None


def test_the_first_contact_requires_an_instance_manifest(instance: Path) -> None:
    johanna = _johanna_settings(portable_precheckout_first_contact_enabled=True)
    # El binding v1 (COMMERCIAL_ALLY_CONFIG_PATH) tampoco alcanza.
    binding_v1 = replace(
        _settings(_att1_manifest(), portable_precheckout_first_contact_enabled=True),
        instance_manifest=None,
    )

    for settings in (johanna, binding_v1):
        with pytest.raises(
            ValueError, match=f"{_FIRST_CONTACT_FLAG} requires an instance manifest"
        ):
            create_app(settings)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (
            {"lead_precheckout_enabled": False},
            f"{_FIRST_CONTACT_FLAG} requires LEAD_PRECHECKOUT_ENABLED or "
            "GHL_PRECHECKOUT_ADAPTER_ENABLED",
        ),
        (
            # Con la salida durable prendida, la frontera ya la exige esa guarda.
            {"pilot_boundary_enabled": False, "dispatcher_outbound_enabled": False},
            f"{_FIRST_CONTACT_FLAG} requires LANCEMOS_PILOT_BOUNDARY_ENABLED",
        ),
        (
            {"pilot_boundary_enabled": False},
            "DURABLE_OUTBOUND_ENABLED requires LANCEMOS_PILOT_BOUNDARY_ENABLED",
        ),
        (
            {"dispatcher_enabled": False},
            f"{_FIRST_CONTACT_FLAG} requires DURABLE_DISPATCHER_ENABLED",
        ),
        (
            {"dispatcher_approved_template_direct_enabled": False},
            f"{_FIRST_CONTACT_FLAG} requires DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED",
        ),
        (
            {"pilot_precheckout_scope_key": None},
            "LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY is required",
        ),
        (
            {"pilot_precheckout_scope_version": None},
            "LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION must be positive",
        ),
        (
            {"pilot_precheckout_scope_version": 0},
            "LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION must be positive",
        ),
        (
            # Un scope publicado tiene una sola fuente: el de recuperacion es
            # de Hotmart y el del primer contacto es de la landing.
            {"pilot_precheckout_scope_key": "att1-recuperacion"},
            "LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY must differ from LANCEMOS_PILOT_SCOPE_KEY",
        ),
        (
            {"waba_precheckout_template_name": None},
            "WABA_PRECHECKOUT_TEMPLATE_NAME is required",
        ),
        (
            {"waba_precheckout_template_name": "att1_carrito_abandonado_01"},
            "WABA_PRECHECKOUT_TEMPLATE_NAME must match plantillas.precheckout.nombre",
        ),
        (
            {"portable_hotmart_purchase_stop_enabled": False, "hotmart_hottok": None},
            f"{_FIRST_CONTACT_FLAG} requires PORTABLE_HOTMART_PURCHASE_STOP_ENABLED$",
        ),
        (
            # Con el hottok cargado y sin el freno, el runtime con manifiesto
            # ya no arrancaba: el hottok solo es portable con el freno.
            {"portable_hotmart_purchase_stop_enabled": False},
            "runtime capabilities are not portable: hotmart_hottok$",
        ),
        (
            {"hotmart_hottok": None},
            f"{_FIRST_CONTACT_FLAG} requires HOTMART_HOTTOK$",
        ),
    ],
)
def test_the_first_contact_does_not_start_without_what_it_needs(
    instance: Path, override: dict[str, object], message: str
) -> None:
    settings = _first_contact_settings(instance, **override)

    with pytest.raises(ValueError, match=message):
        _first_contact_app(settings)


def test_the_first_contact_requires_the_scoped_inbound(instance: Path) -> None:
    # Sin el entrante scoped nadie atiende la respuesta ni guarda el «No mas
    # mensajes». El resto del entrante (Corte B con el remitente fijo, opt-out,
    # derivacion) sigue prendido: lo que falta es el modo scoped.
    settings = _first_contact_settings(
        instance,
        chatwoot_scoped_inbound_senders_enabled=False,
        allowed_jid=None,
    )

    with pytest.raises(
        ValueError,
        match=f"{_FIRST_CONTACT_FLAG} requires CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED",
    ):
        _first_contact_app(settings)


def test_the_first_contact_template_reaches_the_config_only_with_the_flag(
    instance: Path,
) -> None:
    manifest = _with_parameters(_first_contact_manifest(), precheckout=("nombre",))
    on = replace(_first_contact_settings(instance), instance_manifest=manifest)
    off = replace(on, portable_precheckout_first_contact_enabled=False)

    template = _waba_template_config(on)
    template_off = _waba_template_config(off)

    assert template is not None and template_off is not None
    assert template.precheckout_name == _FIRST_CONTACT_TEMPLATE
    assert template.precheckout_body_parameters == ("nombre",)
    assert template.params(
        content="copy", followup=False, buyer_name="Ana", product_name="ATT1",
        trigger_kind="precheckout_intent",
    ) == {
        "name": _FIRST_CONTACT_TEMPLATE,
        "category": "MARKETING",
        "language": "es_MX",
        "processed_params": {"body": {"1": "Ana"}},
    }
    # Apagado, el flujo no tiene plantilla: no presta ni el nombre ni las
    # variables, y nunca sale con la del carrito.
    assert template_off.precheckout_name is None
    assert template_off.precheckout_body_parameters is None
    assert template_off.first_touch_name_for(trigger_kind="precheckout_intent") is None
    # El carrito y el pago fallido no cambian con el flag.
    for kind in ("cart_abandonment", "payment_failure"):
        assert template.first_touch_name_for(
            trigger_kind=kind
        ) == template_off.first_touch_name_for(trigger_kind=kind)


def test_the_first_contact_template_must_share_the_language(instance: Path) -> None:
    manifest = _first_contact_manifest()
    templates = dict(manifest.templates)
    templates["precheckout"] = Template(name=_FIRST_CONTACT_TEMPLATE, language="es_AR")
    settings = replace(
        _first_contact_settings(instance),
        instance_manifest=replace(manifest, templates=templates),
    )

    with pytest.raises(ValueError, match="plantillas.precheckout.idioma"):
        _first_contact_app(settings)


def test_the_first_contact_template_requires_an_instance_manifest() -> None:
    # Johanna corre sin manifiesto: no tiene flujo que mande esa plantilla.
    settings = replace(
        Settings(
            webhook_secret="test-secret",
            allowed_jid=None,
            capture_dir=Path("/tmp/instance-wiring-captures"),
            max_age_seconds=300,
        ),
        **{**_WABA_OUTBOUND, "waba_precheckout_template_name": _FIRST_CONTACT_TEMPLATE},
    )

    with pytest.raises(ValueError, match="WABA_PRECHECKOUT_TEMPLATE_NAME requires an instance manifest"):
        _waba_template_config(settings)


def test_readiness_reports_the_first_contact_scope_only_with_the_flag(
    instance: Path,
) -> None:
    authority = _FirstContactAuthority(runtime_state="armed", reason_code="pilot_runtime_armed")
    on = _first_contact_settings(instance)
    # Apagado se saca tambien lo que solo el flag habilita: sin el, el modo
    # directo no tiene flujo portable y la salida durable pediria a Hermes.
    off = replace(
        on,
        portable_precheckout_first_contact_enabled=False,
        dispatcher_approved_template_direct_enabled=False,
        dispatcher_outbound_enabled=False,
    )
    off_authority = _FirstContactAuthority()

    ready_on = _get_ready(_first_contact_app(on, authority))
    ready_off = _get_ready(_first_contact_app(off, off_authority))

    assert ready_on.status_code == 200 and ready_off.status_code == 200
    assert ready_on.json()["portable_precheckout_first_contact"] == "armed"
    assert "portable_precheckout_first_contact" not in ready_off.json()
    assert off_authority.first_contact_boundaries == []
    # Lo que se consulta es el scope del formulario, con el tenant y el canal
    # del piloto; el resto del payload no cambia.
    [boundary] = authority.first_contact_boundaries
    assert (boundary.scope_key, boundary.scope_version) == (_FIRST_CONTACT_SCOPE, 1)  # type: ignore[attr-defined]
    assert (
        boundary.tenant_key, boundary.channel_provider, boundary.channel_account_ref  # type: ignore[attr-defined]
    ) == ("lancemos", "waba", "chatwoot-inbox:11")
    assert {
        key: value
        for key, value in ready_on.json().items()
        if key != "portable_precheckout_first_contact"
    } == ready_off.json()
    # precheckout_delayed_first_touch es de Johanna y no se toca.
    assert ready_on.json()["precheckout_delayed_first_touch"] == "disabled"


@pytest.mark.parametrize(
    "reason",
    ["pilot_scope_config_mismatch", "pilot_active_scope_mismatch"],
)
def test_readiness_fails_when_the_first_contact_scope_is_not_configured(
    instance: Path, reason: str
) -> None:
    # Un scope sin publicar, de otra fuente, manual_cohort o con otra version
    # activa: el flujo no puede planificar ni mandar.
    authority = _FirstContactAuthority(
        configured=False, runtime_state=None, runtime_generation=None, reason_code=reason
    )

    response = _get_ready(_first_contact_app(_first_contact_settings(instance), authority))

    assert response.status_code == 503
    assert response.json()["detail"] == f"portable_precheckout_{reason}"


def test_readiness_fails_when_the_first_contact_scope_cannot_be_read(
    instance: Path,
) -> None:
    class _Unavailable(_FirstContactAuthority):
        async def get_portable_precheckout_pilot_runtime_status(
            self, *, pilot_boundary: object
        ) -> object:
            raise RuntimeError("database down")

    response = _get_ready(
        _first_contact_app(_first_contact_settings(instance), _Unavailable())
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "portable_precheckout_readiness_unavailable"


def test_johanna_readiness_has_no_first_contact_key() -> None:
    settings = _johanna_settings()

    assert settings.portable_precheckout_first_contact_enabled is False
    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert "portable_precheckout_first_contact" not in response.json()
    assert response.json()["precheckout_delayed_first_touch"] == "disabled"


# ------------------------------------------------------------------ readiness


class _BindingAuthority:
    async def resolve_commercial_ally_runtime_binding(self, config: object) -> object:
        return config


def test_readiness_reports_the_instance_and_knowledge(instance: Path) -> None:
    path = instance / "conocimiento" / "knowledge-v1.toml"
    _approve(path)
    knowledge = CommercialKnowledge.from_toml_file(path)
    settings = _settings(_att1_manifest(), commercial_knowledge=knowledge)

    with TestClient(create_app(settings, supabase_client=_BindingAuthority())) as client:  # type: ignore[arg-type]
        response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["instance_ally"] == "att1"
    assert body["instance_product_version"] == "v1.0.0"
    assert body["commercial_knowledge"] == f"v1:{knowledge.rendered_sha256}"


def _ready(settings: Settings) -> dict[str, str]:
    with TestClient(create_app(settings, supabase_client=_BindingAuthority())) as client:  # type: ignore[arg-type]
        response = client.get("/ready")
    assert response.status_code == 200
    return response.json()


def test_readiness_counts_the_ghl_forms_only_with_the_flag_on() -> None:
    off = _ready(_settings(_ghl_manifest()))
    one = _ready(_ghl_settings())
    two = _ready(_ghl_settings(_ghl_manifest(forms=(_GHL_FORM, _GHL_LANDING_D_FORM))))

    assert "ghl_precheckout_adapter" not in off
    assert one["ghl_precheckout_adapter"] == "enabled:1-forms"
    assert two["ghl_precheckout_adapter"] == "enabled:2-forms"
    # Los ids de los formularios no se publican, y el resto del payload no cambia.
    assert _GHL_FORM not in json.dumps(two)
    assert {key: value for key, value in one.items() if key != "ghl_precheckout_adapter"} == off


def test_readiness_reports_the_form_landing_mode_only_when_declared() -> None:
    # landing_por_formulario (2026-10-09): el modo y cuantos formularios declara,
    # sin sus ids, y solo con el flag prendido, como el conteo de formularios.
    forms = (_GHL_FORM, _GHL_LANDING_D_FORM)
    declared_manifest = replace(
        _ghl_manifest(forms=forms),
        ghl_form_landings=GhlFormLandings(
            mode="sombra", landing_by_form={_GHL_LANDING_D_FORM: "alimenta-tu-tiroides-d"}
        ),
    )
    plain = _ready(_ghl_settings(_ghl_manifest(forms=forms)))
    declared = _ready(_ghl_settings(declared_manifest))
    flag_off = _ready(_settings(declared_manifest))

    assert "ghl_form_landings" not in plain
    assert declared["ghl_form_landings"] == "sombra:1"
    assert "ghl_form_landings" not in flag_off
    assert _GHL_LANDING_D_FORM not in json.dumps(declared)
    assert {key: value for key, value in declared.items() if key != "ghl_form_landings"} == plain


def test_readiness_reports_the_adapter_risk_of_the_manifest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # La clave cuelga de [adaptadores.ghl], con el flag prendido o apagado.
    with caplog.at_level("WARNING", logger="bridge.app"):
        not_accepted = [_ready(_ghl_settings()), _ready(_settings(_ghl_manifest()))]
        accepted = [
            _ready(_ghl_settings(_ghl_manifest(accepted=True))),
            _ready(_settings(_ghl_manifest(accepted=True))),
        ]

    assert [ready["ghl_adapter_risk"] for ready in not_accepted] == ["not_accepted"] * 2
    assert [ready["ghl_adapter_risk"] for ready in accepted] == [
        "accepted:2026-10-01:ghl-precheckout-adapter-v1"
    ] * 2
    # /ready no lleva datos personales: el nombre de quien acepta va solo al log
    # de arranque (D11), una vez por arranque.
    for ready in accepted:
        assert _TEST_RISK_ACCEPTANCE.accepted_by not in json.dumps(ready)
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("ghl_adapter_risk ")
    ]
    assert messages == [
        "ghl_adapter_risk acceptance=absent gated=precheckout,pago_fallido,consented_audience"
    ] * 2 + [
        "ghl_adapter_risk acceptance=accepted by=aceptacion de prueba (tests) "
        "on=2026-10-01 contract=ghl-precheckout-adapter-v1"
    ] * 2
    assert {record.levelname for record in caplog.records if "ghl_adapter_risk" in record.getMessage()} == {"WARNING"}


def test_without_the_section_and_without_a_gated_flow_readiness_does_not_change(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # El fixture de ATT1 (sin intenciones), una instancia que recibe intenciones
    # sin un flujo que las use como permiso, y Johanna: ni clave ni aviso.
    with caplog.at_level("WARNING", logger="bridge.app"):
        plain = _ready(_settings(_att1_manifest()))
        intents_only = _ready(_settings(_ghl_manifest(forms=(), carrito=True)))
        with TestClient(create_app(_johanna_settings())) as client:
            johanna = client.get("/ready").json()

    for ready in (plain, intents_only, johanna):
        assert "ghl_adapter_risk" not in ready
    assert not [r for r in caplog.records if "ghl_adapter_risk" in r.getMessage()]


# ------------------------------------- guarda de audiencia del adaptador de GHL
# Con [adaptadores.ghl] y sin la aceptacion, el scope del piloto no puede tener
# una audiencia con consentimiento (consented_intent, consented_intent_in_cohort):
# usa la intencion del formulario como audiencia. El modo vive en la base, asi que
# se lee en el arranque (antes de cualquier worker) y en /ready, y falla cerrado.
# El set es el de ATT1 con el carrito prendido: el flujo que queda disponible sin
# la aceptacion.

_WORKERS = (
    "durable_dispatcher",
    "opt_out_projection_worker",
    "human_handoff_projection_worker",
    "chatwoot_worker",
)


class _AudienceAuthority(_FirstContactAuthority):
    """La base de /ready mas la lectura del modo de audiencia del scope."""

    def __init__(self, mode: object = "manual_cohort") -> None:
        super().__init__()
        self.mode = mode
        self.audience_boundaries: list[object] = []

    async def get_pilot_scope_audience_mode(self, *, pilot_boundary: object) -> object:
        self.audience_boundaries.append(pilot_boundary)
        if isinstance(self.mode, Exception):
            raise self.mode
        return self.mode


def _cart_settings(
    instance: Path, *, adapter: bool = True, accepted: bool = False, **overrides: object
) -> Settings:
    """ATT1 con el carrito y el entrante prendidos y la frontera del piloto."""
    base = _first_contact_settings(instance)
    manifest = _ghl_manifest(
        forms=(_GHL_FORM,) if adapter else (),
        accepted=accepted,
        carrito=True,
        inbound=True,
    )
    values: dict[str, object] = {
        "instance_manifest": manifest,
        "portable_precheckout_first_contact_enabled": False,
        "pilot_precheckout_scope_key": None,
        "pilot_precheckout_scope_version": None,
        "waba_precheckout_template_name": None,
        "portable_hotmart_recovery_enabled": True,
    }
    values.update(overrides)
    return replace(base, **values)


def _run_lifespan(app: object, started: list[str] | None = None) -> list[str]:
    """Corre el arranque y el cierre; anota en ``started`` los workers que arrancan."""
    started = [] if started is None else started
    for name in _WORKERS:
        worker = getattr(app.state, name)  # type: ignore[attr-defined]
        assert worker is not None, name

        async def start(name: str = name) -> None:
            started.append(name)

        worker.start = start

    async def run() -> None:
        async with app.router.lifespan_context(app):  # type: ignore[attr-defined]
            pass

    try:
        asyncio.run(run())
    finally:
        for name in _WORKERS:
            del getattr(app.state, name).start  # type: ignore[attr-defined]
    return started


def test_a_manual_cohort_scope_starts_without_the_acceptance(instance: Path) -> None:
    authority = _AudienceAuthority("manual_cohort")
    app = _first_contact_app(_cart_settings(instance), authority)

    started = _run_lifespan(app)
    ready = _get_ready(app)

    assert started == list(_WORKERS)
    assert ready.status_code == 200
    assert ready.json()["ghl_adapter_risk"] == "not_accepted"
    assert ready.json()["pilot_boundary"] == "configured"
    # Una lectura en el arranque y otra en /ready, del scope de recuperacion.
    assert len(authority.audience_boundaries) == 2
    for boundary in authority.audience_boundaries:
        assert (boundary.scope_key, boundary.scope_version) == ("att1-recuperacion", 1)  # type: ignore[attr-defined]


@pytest.mark.parametrize("mode", ["consented_intent", "consented_intent_in_cohort"])
def test_a_consented_audience_does_not_start_without_the_acceptance(
    instance: Path, mode: str
) -> None:
    authority = _AudienceAuthority(mode)
    app = _first_contact_app(_cart_settings(instance), authority)

    started: list[str] = []

    with pytest.raises(RuntimeError, match="^ghl_adapter_risk_not_accepted: "):
        _run_lifespan(app, started)
    ready = _get_ready(app)

    # Ningun worker llego a arrancar: los workers corren aunque /ready de 503.
    assert started == []
    assert len(authority.audience_boundaries) == 2

    assert ready.status_code == 503
    assert ready.json()["detail"] == "ghl_adapter_risk_not_accepted"


@pytest.mark.parametrize(
    "mode",
    [
        SupabaseError("pilot_scope_audience_mode_failed: HTTP 503"),
        SupabaseError("pilot_scope_audience_mode_scope_not_published"),
        RuntimeError("database down"),
        None,
        "everyone",
        "",
    ],
    ids=["rpc-503", "sin-publicar", "caida", "null", "modo-desconocido", "vacio"],
)
def test_an_audience_that_cannot_be_read_blocks_without_the_acceptance(
    instance: Path, mode: object
) -> None:
    # Falla cerrado: sin poder leer el modo no se sabe si la audiencia usa
    # intenciones del adaptador.
    authority = _AudienceAuthority(mode)
    app = _first_contact_app(_cart_settings(instance), authority)
    started: list[str] = []

    with pytest.raises(RuntimeError, match="^ghl_adapter_risk_audience_unavailable: "):
        _run_lifespan(app, started)
    ready = _get_ready(app)

    assert started == []
    assert ready.status_code == 503
    assert ready.json()["detail"] == "ghl_adapter_risk_audience_unavailable"


def test_a_base_without_the_read_migration_blocks_without_the_acceptance(
    instance: Path,
) -> None:
    # El cliente sin el metodo es el doble de una base sin la migracion
    # 20261001000300: tampoco arranca.
    app = _first_contact_app(_cart_settings(instance), _FirstContactAuthority())

    with pytest.raises(RuntimeError, match="^ghl_adapter_risk_audience_unavailable: "):
        _run_lifespan(app)
    assert _get_ready(app).json()["detail"] == "ghl_adapter_risk_audience_unavailable"


def test_without_a_supabase_client_the_audience_gate_blocks() -> None:
    # Sin Supabase /ready ya responde 503 por la frontera del piloto; el arranque
    # tambien corta, sin poder leer el modo.
    boundary = PilotBoundaryConfig(
        scope_key="att1-recuperacion",
        scope_version=1,
        tenant_key="lancemos",
        channel_provider="waba",
        channel_account_ref="chatwoot-inbox:11",
    )

    assert (
        asyncio.run(_ghl_adapter_audience_block(None, boundary))
        == "ghl_adapter_risk_audience_unavailable"
    )


def test_the_pilot_readiness_reason_comes_before_the_audience_gate(instance: Path) -> None:
    # Un scope sin configurar ya responde 503 con su motivo, como hoy.
    class _NotConfigured(_AudienceAuthority):
        async def get_pilot_runtime_status(self, *, pilot_boundary: object) -> object:
            return SimpleNamespace(
                configured=False, runtime_state=None, runtime_generation=None,
                reason_code="pilot_scope_config_mismatch",
            )

    authority = _NotConfigured("consented_intent")
    ready = _get_ready(_first_contact_app(_cart_settings(instance), authority))

    assert ready.status_code == 503
    assert ready.json()["detail"] == "pilot_scope_config_mismatch"
    assert authority.audience_boundaries == []


@pytest.mark.parametrize("mode", ["consented_intent", "consented_intent_in_cohort"])
def test_with_the_acceptance_the_audience_is_not_read(instance: Path, mode: str) -> None:
    authority = _AudienceAuthority(mode)
    app = _first_contact_app(_cart_settings(instance, accepted=True), authority)

    started = _run_lifespan(app)
    ready = _get_ready(app)

    assert started == list(_WORKERS)
    assert ready.status_code == 200
    assert ready.json()["ghl_adapter_risk"] == "accepted:2026-10-01:ghl-precheckout-adapter-v1"
    assert authority.audience_boundaries == []


def test_without_the_adapter_section_the_audience_is_not_read(instance: Path) -> None:
    # Una instancia que nunca declaro el adaptador: su audiencia con consentimiento
    # es de su landing. No hay lectura ni clave.
    authority = _AudienceAuthority("consented_intent")
    app = _first_contact_app(_cart_settings(instance, adapter=False), authority)

    started = _run_lifespan(app)
    ready = _get_ready(app)

    assert started == list(_WORKERS)
    assert ready.status_code == 200
    assert "ghl_adapter_risk" not in ready.json()
    assert authority.audience_boundaries == []


def test_without_the_pilot_boundary_the_audience_is_not_read() -> None:
    # ATT1 hoy: el adaptador prendido, sin aceptacion y con la frontera apagada.
    authority = _AudienceAuthority("consented_intent")
    app = create_app(_ghl_settings(), supabase_client=authority)  # type: ignore[arg-type]

    with TestClient(app) as client:
        ready = client.get("/ready")

    assert ready.status_code == 200
    assert ready.json()["pilot_boundary"] == "disabled"
    assert ready.json()["ghl_adapter_risk"] == "not_accepted"
    assert authority.audience_boundaries == []


# -------------------------------------------------------- guarda de medicacion


def test_default_guard_is_the_one_johanna_runs_today() -> None:
    assert _MEDICATION_GUIDANCE_SUBJECT_RE.pattern == (
        r"\b(?:medicacion|medicamento|farmaco|pastilla|antidepresiv|ansiolitic)\w*\b"
    )
    assert _MEDICATION_GUIDANCE_ACTION_RE.pattern == (
        r"\b(?:dejar|suspender|interrumpir|cambiar|reducir|aumentar|tomar|dosis|dosificacion)\w*\b"
    )
    assert _requires_medication_guidance_handoff("¿Puedo dejar los antidepresivos?")


def test_att1_guard_catches_thyroid_medication_that_the_default_misses() -> None:
    manifest = _att1_manifest()
    subject_re = _stem_pattern(manifest.sensitive_subjects)
    action_re = _stem_pattern(manifest.sensitive_actions)
    message = "¿Puedo dejar la Levotiroxina si hago el programa?"

    assert not _requires_medication_guidance_handoff(message)
    assert _requires_medication_guidance_handoff(
        message, subject_re=subject_re, action_re=action_re
    )
    assert not _requires_medication_guidance_handoff(
        "¿Cuánto cuesta el programa?", subject_re=subject_re, action_re=action_re
    )


# --------------------------------------------------------------------- Hermes


def _capturing_processor(tmp_path: Path, system_prompt: str | None) -> tuple[HermesShadowProcessor, list[dict]]:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(503, json={"detail": "unavailable"})

    processor = HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="att1-agente-comercial",
        shadow_dir=tmp_path,
        transport=httpx.MockTransport(handler),
        system_prompt=system_prompt,
    )
    return processor, requests


def test_knowledge_travels_as_system_before_the_context(tmp_path: Path, instance: Path) -> None:
    path = instance / "conocimiento" / "knowledge-v1.toml"
    _approve(path)
    block = CommercialKnowledge.from_toml_file(path).render()
    processor, requests = _capturing_processor(tmp_path, block)

    asyncio.run(processor.run(delivery_id="d1", context={"messages": []}))

    messages = requests[0]["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[0]["content"] == block
    assert json.loads(messages[1]["content"]) == {"messages": []}


def test_without_knowledge_the_request_is_unchanged(tmp_path: Path) -> None:
    processor, requests = _capturing_processor(tmp_path, None)

    asyncio.run(processor.run(delivery_id="d2", context={"messages": []}))

    assert [message["role"] for message in requests[0]["messages"]] == ["user"]
