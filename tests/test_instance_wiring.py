"""El bridge con manifiesto de instancia v2 y conocimiento comercial (F2b).

Fixtures: tests/fixtures/instances/att1 (datos de ATT1 medidos el 2026-09-28).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.app import (
    Settings,
    _MEDICATION_GUIDANCE_ACTION_RE,
    _MEDICATION_GUIDANCE_SUBJECT_RE,
    _requires_medication_guidance_handoff,
    _stem_pattern,
    _waba_template_config,
    create_app,
)
from bridge.commercial_knowledge import CommercialKnowledge, KnowledgeError
from bridge.hermes import HermesShadowProcessor
from bridge.instance_manifest import InstanceManifest, Template

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


def _ghl_manifest(
    *, intencion: bool = True, forms: tuple[str, ...] = (_GHL_FORM,)
) -> InstanceManifest:
    manifest = _att1_manifest()
    events = manifest.events | {"intencion"} if intencion else manifest.events
    return replace(manifest, events=frozenset(events), ghl_form_ids=forms)


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


@pytest.mark.parametrize("token", [None, "x" * 31])
def test_the_ghl_adapter_needs_a_token_of_32_characters(token: str | None) -> None:
    with pytest.raises(ValueError, match="GHL_PRECHECKOUT_ADAPTER_TOKEN must contain"):
        create_app(_ghl_settings(ghl_precheckout_adapter_token=token))


def test_a_ghl_adapter_token_of_exactly_32_characters_builds() -> None:
    assert create_app(_ghl_settings(ghl_precheckout_adapter_token="x" * 32)) is not None


@pytest.mark.parametrize("secret", ["lead_precheckout_secret", "webhook_secret"])
def test_the_ghl_adapter_token_must_differ_from_the_other_secrets(secret: str) -> None:
    settings = _ghl_settings(**{secret: _GHL_TOKEN})

    with pytest.raises(ValueError, match="GHL_PRECHECKOUT_ADAPTER_TOKEN must differ"):
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
