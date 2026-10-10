"""El cableado del seguimiento con cupon en el bridge.

Lo que no depende de una instancia: las variables, sus guardas, el binding v1 y
el barredor de Johanna (sin manifiesto). El arranque con el manifiesto de ATT1
esta en tests/test_instance_wiring.py.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from bridge import app as app_module
from bridge.app import Settings, create_app
from bridge.chatwoot import ChatwootClient
from bridge.followup_discount import ConversationFollowupSweeper
from bridge.instance_manifest import InstanceManifest

ATT1 = Path(__file__).parent / "fixtures" / "instances" / "att1"
# Los seis parametros del barredor que existen para el runtime con manifiesto.
MANIFEST_SWEEPER_OPTIONS = (
    "only_phone",
    "send_hours",
    "time_zone",
    "refuse_unsafe_greeting",
    "phone_equivalence",
    "external_user_id_resolver",
)
# Sintetico, con la forma del wa_id de Mexico (la del contacto de F4).
TEST_PHONE = "+5215500000099"
# El mensaje de un horario que no se puede usar, por la forma o por el rango:
# nombra la variable y lo que espera, a diferencia del generico ('invalid
# conversation followup configuration'), que comparten el intervalo, la
# ventana, el tope y las paginas.
SEND_HOURS_ERROR = (
    "^CONVERSATION_FOLLOWUP_SEND_HOURS must be HH-HH with 00 <= start < end <= 24 "
    "in the time zone of the instance manifest, for example 09-21, or 00-24 to "
    "send at any hour$"
)


class _FakeSupabase:
    async def admit_johanna_funnel_event(self, **_: object) -> object:
        return SimpleNamespace(outcome="inserted", event_id="x")


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "webhook_secret": "unused",
        "allowed_jid": None,
        "capture_dir": Path("/tmp/followup-discount-wiring-tests"),
        "max_age_seconds": 300,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _followup_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "conversation_followup_enabled": True,
        "conversation_followup_template_name": "johanna_seguimiento_descuento_01",
        "conversation_followup_coupon_code": "JOHANNA10",
        "conversation_followup_product_name": "Libérate de la Ansiedad",
        "chatwoot_account_id": 1,
        "chatwoot_inbox_id": 9,
        "chatwoot_agent_bot_access_token": "agent-bot-token",
        "agent_bot_id": 1,
        "chatwoot_cut_b_admission_enabled": True,
        "supabase_base_url": "https://supabase.example.test",
        "supabase_service_role_key": "service-role",
        "allowed_jid": "12025550123@s.whatsapp.net",
    }
    values.update(overrides)
    return _settings(**values)


def _johanna_followup_settings(**overrides: object) -> Settings:
    """El set con el que Johanna arranca el seguimiento: el de arriba con el
    scope de Corte B de su binding (libre-de-ansiedad-inbound v2)."""
    return _followup_settings(
        chatwoot_cut_b_scope_key="libre-de-ansiedad-inbound",
        chatwoot_cut_b_scope_version=2,
        **overrides,
    )


def _chatwoot_client(account_id: int) -> ChatwootClient:
    # El barredor exige un ChatwootClient de verdad; ningun test de aca llega
    # a Chatwoot (sin lifespan el barredor no corre).
    return ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=account_id,
        access_token="test-control-token",
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(404)),
    )


def _get_ready(app: object) -> httpx.Response:
    # Sin lifespan: los workers no arrancan, solo se consulta /ready.
    async def get() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/ready")

    return asyncio.run(get())


def _settings_from_env(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name, value in {
        "CHATWOOT_WEBHOOK_SECRET": "unused",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "unused",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
    }.items():
        monkeypatch.setenv(name, value)
    return Settings.from_env()


def test_followup_settings_default_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CONVERSATION_FOLLOWUP_ONLY_PHONE", "CONVERSATION_FOLLOWUP_SEND_HOURS"):
        monkeypatch.delenv(name, raising=False)
    settings = _settings_from_env(monkeypatch)
    assert settings.conversation_followup_enabled is False
    assert settings.conversation_followup_coupon_code is None
    # 24 a 72 h desde el ultimo mensaje del lead (decision de Dan, 2026-09-28).
    assert settings.conversation_followup_min_age_seconds == 86_400
    assert settings.conversation_followup_max_age_seconds == 259_200
    assert settings.conversation_followup_interval_seconds == 300.0
    # Sin modo de prueba y sin horario: como corre Johanna.
    assert settings.conversation_followup_only_phone is None
    assert settings.conversation_followup_send_hours is None


def test_followup_settings_are_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_ENABLED", "true")
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_TEMPLATE_NAME", "johanna_seguimiento_descuento_01")
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_TEMPLATE_LANGUAGE", "es_EC")
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_COUPON_CODE", "JOHANNA10")
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_PRODUCT_NAME", "Libérate de la Ansiedad")
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_MAX_AGE_SECONDS", "172800")
    settings = _settings_from_env(monkeypatch)
    assert settings.conversation_followup_enabled is True
    assert settings.conversation_followup_template_name == "johanna_seguimiento_descuento_01"
    assert settings.conversation_followup_template_language == "es_EC"
    assert settings.conversation_followup_coupon_code == "JOHANNA10"
    assert settings.conversation_followup_product_name == "Libérate de la Ansiedad"
    assert settings.conversation_followup_max_age_seconds == 172_800


def test_the_test_phone_and_the_send_hours_are_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_ONLY_PHONE", f" {TEST_PHONE} ")
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_SEND_HOURS", " 09-21 ")
    settings = _settings_from_env(monkeypatch)
    assert settings.conversation_followup_only_phone == TEST_PHONE
    assert settings.conversation_followup_send_hours == (9, 21)

    # A cualquier hora tambien se escribe.
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_SEND_HOURS", "00-24")
    assert _settings_from_env(monkeypatch).conversation_followup_send_hours == (0, 24)

    # Vacias, como quedan al abrir el seguimiento a todo el inbox.
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_ONLY_PHONE", "")
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_SEND_HOURS", "   ")
    settings = _settings_from_env(monkeypatch)
    assert settings.conversation_followup_only_phone is None
    assert settings.conversation_followup_send_hours is None


@pytest.mark.parametrize(
    "value",
    ["9-21", "09:00-21:00", "0921", "09 - 21", "09-21h", "09-21,22-23", "nueve-veintiuno"],
)
def test_send_hours_that_are_not_hh_hh_do_not_start(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_SEND_HOURS", value)
    with pytest.raises(ValueError, match=SEND_HOURS_ERROR):
        _settings_from_env(monkeypatch)


@pytest.mark.parametrize(
    "overrides",
    [
        {"conversation_followup_template_name": None},
        {"conversation_followup_coupon_code": None},
        {"conversation_followup_coupon_code": "JOHANNA 10"},
        {"conversation_followup_product_name": None},
        {"chatwoot_agent_bot_access_token": None},
        {"chatwoot_cut_b_admission_enabled": False},
        {"allowed_jid": None, "chatwoot_scoped_inbound_senders_enabled": False},
    ],
)
def test_enabling_the_followup_half_configured_refuses_to_start(
    overrides: dict[str, object],
) -> None:
    # Un cupon es un efecto externo irreversible: sin plantilla, sin cupon o sin
    # la admision de la que sale el link, el bridge no arranca a medias.
    with pytest.raises(ValueError) as error:
        create_app(
            _followup_settings(**overrides),
            supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
        )
    assert "CONVERSATION_FOLLOWUP_" in str(error.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"conversation_followup_interval_seconds": 0.0},
        {"conversation_followup_max_sends_per_scan": 0},
        {"conversation_followup_max_pages": 21},
        {
            "conversation_followup_min_age_seconds": 100,
            "conversation_followup_max_age_seconds": 10,
        },
    ],
)
def test_invalid_followup_configuration_refuses_to_start_even_disabled(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError) as error:
        create_app(
            _settings(conversation_followup_enabled=False, **overrides),
            supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
        )
    assert "conversation followup configuration" in str(error.value)


@pytest.mark.parametrize(
    "send_hours",
    [
        # 0 <= inicio < fin <= 24, en horas enteras. Las cuatro primeras tienen
        # la forma HH-HH: armadas a mano no pasan por la lectura del entorno.
        (21, 9),
        (9, 9),
        (0, 25),
        (24, 24),
        (9, 21.5),
        (9, 15, 21),
    ],
)
def test_send_hours_out_of_range_refuse_to_start_even_disabled(
    send_hours: object,
) -> None:
    with pytest.raises(ValueError, match=SEND_HOURS_ERROR):
        create_app(
            _settings(
                conversation_followup_enabled=False,
                conversation_followup_send_hours=send_hours,
            ),
            supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("value", ["21-09", "09-25", "00-00", "24-24", "09-09"])
def test_send_hours_out_of_range_from_the_environment_do_not_start(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # Tienen la forma HH-HH, pero no hay hora que caiga adentro: el bridge no
    # arranca, y el mensaje dice que variable y que rango (antes era el
    # generico, sin la variable).
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_SEND_HOURS", value)
    with pytest.raises(ValueError, match=SEND_HOURS_ERROR):
        _settings_from_env(monkeypatch)


@pytest.mark.parametrize(
    "phone",
    [
        TEST_PHONE.removeprefix("+"),
        "+0" + TEST_PHONE.removeprefix("+"),
        "+52 1 55 0000 0099",
        "+521-550-000-0099",
        "+123456",
        "+1234567890123456",
        "whatsapp:" + TEST_PHONE,
    ],
)
@pytest.mark.parametrize("enabled", [False, True])
def test_a_test_phone_that_is_not_e164_does_not_start(phone: str, enabled: bool) -> None:
    # Con el flag apagado tambien: un valor mal cargado no espera a que alguien
    # prenda el seguimiento para frenar.
    settings = _johanna_followup_settings(
        conversation_followup_enabled=enabled,
        conversation_followup_only_phone=phone,
    )
    with pytest.raises(ValueError, match="^CONVERSATION_FOLLOWUP_ONLY_PHONE must be an E.164 phone$"):
        create_app(
            settings,
            chatwoot_client=_chatwoot_client(1),
            supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("send_hours", [(9, 21), (0, 24)])
@pytest.mark.parametrize("enabled", [False, True])
def test_send_hours_without_a_manifest_do_not_start(
    send_hours: tuple[int, int], enabled: bool
) -> None:
    # Sin manifiesto no hay zona horaria en la que leer el horario. Johanna
    # manda a cualquier hora sin escribirlo.
    settings = _johanna_followup_settings(
        conversation_followup_enabled=enabled,
        conversation_followup_send_hours=send_hours,
    )
    with pytest.raises(
        ValueError, match="^CONVERSATION_FOLLOWUP_SEND_HOURS requires an instance manifest"
    ):
        create_app(
            settings,
            chatwoot_client=_chatwoot_client(1),
            supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
        )


def test_the_followup_is_a_portable_capability() -> None:
    # Desde la 1.5.0: con manifiesto reserva por claim_portable_conversation_
    # followup_v1 y resuelve la identidad como el entrante. Antes su reserva
    # dependia del catalogo de Johanna y el flag frenaba todo runtime portable.
    assert "conversation_followup_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES


def _att1_v1_binding_settings(**overrides: object) -> Settings:
    """ATT1 como corria antes del manifiesto v2: el binding v1 en JSON
    (COMMERCIAL_ALLY_CONFIG_PATH), con el set completo del seguimiento."""
    manifest = InstanceManifest.from_toml_file(ATT1 / "instancia.toml")
    config = manifest.to_commercial_ally_config()
    return _followup_settings(
        commercial_ally_config=config,
        commercial_ally_manifest_path=Path("/runtime/commercial-ally.json"),
        conversation_followup_template_name="att1_seguimiento_descuento_01",
        conversation_followup_template_language="es_MX",
        conversation_followup_coupon_code="TIROIDES10",
        conversation_followup_product_name=manifest.product_name,
        chatwoot_account_id=config.chatwoot_account_id,
        chatwoot_inbox_id=config.chatwoot_inbox_id,
        chatwoot_cut_b_scope_key=config.inbound_scope_key,
        chatwoot_cut_b_scope_version=config.inbound_scope_version,
        **overrides,
    )


def test_the_v1_binding_does_not_start_the_followup() -> None:
    # Sin manifiesto no hay resolvedor ni reserva portable: el cupon saldria
    # por la reserva compartida, con el wa_id textual y sin la barrera de la
    # conversacion adoptada.
    settings = _att1_v1_binding_settings()
    assert settings.instance_manifest is None
    with pytest.raises(
        ValueError,
        match=(
            "^CONVERSATION_FOLLOWUP_ENABLED in a portable runtime requires "
            "INSTANCE_MANIFEST_PATH$"
        ),
    ):
        create_app(
            settings,
            chatwoot_client=_chatwoot_client(settings.chatwoot_account_id or 0),
            supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
        )

    # Lo que frena es el flag: el mismo binding con el flag apagado arranca.
    off = replace(settings, conversation_followup_enabled=False)
    app = create_app(
        off,
        chatwoot_client=_chatwoot_client(settings.chatwoot_account_id or 0),
        supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
    )
    assert app.state.conversation_followup_sweeper is None


def test_johanna_builds_the_sweeper_without_the_resolver_or_the_equivalence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # El constructor real corre: el espia solo mira lo que recibe.
    built: list[dict[str, object]] = []

    def spy(**kwargs: object) -> ConversationFollowupSweeper:
        built.append(kwargs)
        return ConversationFollowupSweeper(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(app_module, "ConversationFollowupSweeper", spy)

    app = create_app(
        _johanna_followup_settings(),
        chatwoot_client=_chatwoot_client(1),
        supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
    )

    [kwargs] = built
    # Los seis parametros del runtime con manifiesto llegan con los defaults
    # del barredor, que son los que lo dejan como estaba: la reserva
    # compartida con la identidad textual, a cualquier hora, a todo el inbox
    # y con el saludo de siempre.
    defaults = {
        name: parameter.default
        for name, parameter in inspect.signature(ConversationFollowupSweeper).parameters.items()
        if name in MANIFEST_SWEEPER_OPTIONS
    }
    assert defaults == {
        "only_phone": None,
        "send_hours": None,
        "time_zone": None,
        "refuse_unsafe_greeting": False,
        "phone_equivalence": False,
        "external_user_id_resolver": None,
    }
    assert {name: kwargs[name] for name in MANIFEST_SWEEPER_OPTIONS} == defaults
    sweeper = app.state.conversation_followup_sweeper
    assert isinstance(sweeper, ConversationFollowupSweeper)
    assert sweeper.only_phone is None and sweeper.send_hours is None

    # /ready de Johanna: las dos claves de siempre, ninguna de las nuevas.
    response = _get_ready(app)
    assert response.status_code == 200
    body = response.json()
    assert {key for key in body if key.startswith("conversation_followup")} == {
        "conversation_followup",
        "conversation_followup_last_scan",
    }
    assert body["conversation_followup"] == "never"
