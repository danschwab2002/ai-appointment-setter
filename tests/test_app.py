"""HTTP contract tests for sanitary Johanna funnel observations."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from bridge.app import Settings, create_app

SECRET = "johanna-funnel-observability-secret"
EVENT_ID = "01K4N9YQ2T7W3H5J8M6P0R1SVC"
SESSION_ID = "01K4N9YQ2T7W3H5J8M6P0R1SVD"


class _FakeSupabase:
    def __init__(self, outcome: str = "inserted") -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    async def admit_johanna_funnel_event(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(outcome=self.outcome, event_id=EVENT_ID)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "webhook_secret": "unused",
        "allowed_jid": None,
        "capture_dir": Path("/tmp/johanna-funnel-observability-tests"),
        "max_age_seconds": 300,
        "lead_precheckout_secret": SECRET,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _settings_from_env(monkeypatch: pytest.MonkeyPatch) -> Settings:
    required_environment = {
        "CHATWOOT_WEBHOOK_SECRET": "unused",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "unused",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
    }
    for name, value in required_environment.items():
        monkeypatch.setenv(name, value)
    return Settings.from_env()


def _payload() -> dict[str, object]:
    return {
        "version": "1.0.0",
        "event_id": EVENT_ID,
        "event_type": "page_view",
        "occurred_at": datetime.now(UTC).isoformat(),
        "anonymous_session_id": SESSION_ID,
        "landing_ref": "ads-a",
        "offer_ref": "bxjge6zq",
        "utm": {
            "source": "meta",
            "medium": "paid_social",
            "campaign": "anxiety_vsl",
            "content": "creative_a",
            "term": None,
        },
    }


def _post(
    app: object,
    payload: object,
    *,
    signature: str | None = None,
    content_type: str = "application/json; charset=utf-8",
) -> httpx.Response:
    body = (
        payload
        if isinstance(payload, bytes)
        else json.dumps(payload, separators=(",", ":")).encode()
    )
    digest = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/webhooks/johanna-funnel-events",
                content=body,
                headers={
                    "Content-Type": content_type,
                    "X-Lancemos-Signature": signature or digest,
                },
            )

    return asyncio.run(send())


def test_signed_closed_event_is_admitted_without_pii_or_payload_envelope() -> None:
    supabase = _FakeSupabase()
    response = _post(create_app(_settings(), supabase_client=supabase), _payload())  # type: ignore[arg-type]

    assert response.status_code == 202
    assert response.json() == {"status": "received", "event_id": EVENT_ID}
    assert supabase.calls == [{
        "version": "1.0.0",
        "event_id": EVENT_ID,
        "event_type": "page_view",
        "occurred_at": supabase.calls[0]["occurred_at"],
        "anonymous_session_id": SESSION_ID,
        "landing_ref": "ads-a",
        "offer_ref": "bxjge6zq",
        "utm_source": "meta",
        "utm_medium": "paid_social",
        "utm_campaign": "anxiety_vsl",
        "utm_content": "creative_a",
        "utm_term": None,
    }]


def test_observability_ignores_removed_dedicated_secret_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOHANNA_FUNNEL_OBSERVABILITY_SECRET", SECRET)
    monkeypatch.delenv("LEAD_PRECHECKOUT_SECRET", raising=False)
    supabase = _FakeSupabase()
    response = _post(
        create_app(
            _settings_from_env(monkeypatch),
            supabase_client=supabase,  # type: ignore[arg-type]
        ),
        _payload(),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "johanna_funnel_not_enabled"
    assert supabase.calls == []


def test_existing_lead_secret_enables_observability_independently_of_lead_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JOHANNA_FUNNEL_OBSERVABILITY_SECRET", raising=False)
    monkeypatch.setenv("LEAD_PRECHECKOUT_SECRET", SECRET)
    monkeypatch.setenv("LEAD_PRECHECKOUT_ENABLED", "false")
    supabase = _FakeSupabase()

    response = _post(
        create_app(
            _settings_from_env(monkeypatch),
            supabase_client=supabase,  # type: ignore[arg-type]
        ),
        _payload(),
    )

    assert response.status_code == 202
    assert len(supabase.calls) == 1


def test_exact_replay_and_semantic_conflict_have_distinct_http_outcomes() -> None:
    duplicate = _post(
        create_app(_settings(), supabase_client=_FakeSupabase("duplicate")),  # type: ignore[arg-type]
        _payload(),
    )
    conflict = _post(
        create_app(_settings(), supabase_client=_FakeSupabase("semantic_conflict")),  # type: ignore[arg-type]
        _payload(),
    )

    assert duplicate.status_code == 200
    assert duplicate.json() == {"status": "duplicate", "event_id": EVENT_ID}
    assert conflict.status_code == 409
    assert conflict.json() == {"status": "conflict", "event_id": EVENT_ID}


def test_invalid_signature_is_rejected_before_json_and_rpc() -> None:
    supabase = _FakeSupabase()
    response = _post(
        create_app(_settings(), supabase_client=supabase),  # type: ignore[arg-type]
        b"not-json",
        signature="sha256=" + "0" * 64,
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_johanna_funnel_signature"
    assert supabase.calls == []


def test_closed_schema_rejects_pii_tracking_and_arbitrary_payload_before_rpc() -> None:
    for field, value in (
        ("email", "private@example.test"),
        ("phone", "+12025550123"),
        ("name", "Private Person"),
        ("fbclid", "tracking-id"),
        ("payload", {"anything": "goes"}),
    ):
        payload = _payload()
        payload[field] = value
        supabase = _FakeSupabase()

        response = _post(
            create_app(_settings(), supabase_client=supabase),  # type: ignore[arg-type]
            payload,
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "invalid_johanna_funnel_event"
        assert supabase.calls == []


def test_event_enums_ulids_pair_and_bounded_utm_fail_closed() -> None:
    mutations = (
        ("event_type", "purchase"),
        ("event_id", "not-a-ulid"),
        ("anonymous_session_id", "01k4n9yq2t7w3h5j8m6p0r1svd"),
        ("offer_ref", "mgbgpp19"),
    )
    for field, value in mutations:
        payload = _payload()
        payload[field] = value
        supabase = _FakeSupabase()
        response = _post(
            create_app(_settings(), supabase_client=supabase),  # type: ignore[arg-type]
            payload,
        )
        assert response.status_code == 400
        assert supabase.calls == []

    payload = _payload()
    payload["utm"]["campaign"] = "x" * 129  # type: ignore[index]
    supabase = _FakeSupabase()
    response = _post(
        create_app(_settings(), supabase_client=supabase),  # type: ignore[arg-type]
        payload,
    )
    assert response.status_code == 400
    assert supabase.calls == []


def test_event_older_than_the_maximum_dashboard_window_is_rejected() -> None:
    payload = _payload()
    payload["occurred_at"] = "2000-01-01T00:00:00Z"
    supabase = _FakeSupabase()

    response = _post(
        create_app(_settings(), supabase_client=supabase),  # type: ignore[arg-type]
        payload,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_johanna_funnel_event"
    assert supabase.calls == []


def test_event_more_than_five_minutes_in_the_future_is_rejected() -> None:
    payload = _payload()
    payload["occurred_at"] = (datetime.now(UTC) + timedelta(minutes=6)).isoformat()
    supabase = _FakeSupabase()

    response = _post(
        create_app(_settings(), supabase_client=supabase),  # type: ignore[arg-type]
        payload,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_johanna_funnel_event"
    assert supabase.calls == []


# ── Levantar la pausa: a quien si y a quien no ─────────────────────────

from bridge.app import _normalize_chatwoot_history, _resume_paused_conversation
from bridge.chatwoot import ChatwootProtocolError
from bridge.supabase import ConversationResumeResult, SupabaseError


AHORA = 1_700_000_000


def _mensajes_con_humano(hace_segundos: int) -> list[dict[str, object]]:
    return [
        {
            "message_type": 1,
            "private": False,
            "content": "yo sigo",
            "sender": {"type": "user", "id": 4},
            "created_at": AHORA - hace_segundos,
        }
    ]


class _ClienteFalso:
    def __init__(self, *, labels=("automation_paused",), status="open",
                 can_reply=True, assignee=False, mensajes=None):
        self._snapshot = SimpleNamespace(
            status=status,
            can_reply=can_reply,
            labels=tuple(labels),
            human_assignee_present=assignee,
        )
        self._mensajes = mensajes if mensajes is not None else []
        self.etiqueta_sacada = False

    async def get_canonical_conversation_snapshot(self, **_: object):
        return self._snapshot

    async def get_conversation_messages(self, **_: object):
        return self._mensajes

    async def clear_conversation_label(self, **_: object) -> bool:
        self.etiqueta_sacada = True
        return True


class _SupabaseFalso:
    def __init__(self, outcome: str = "resumed"):
        self.outcome = outcome
        self.llamadas: list[dict[str, object]] = []

    async def resume_paused_conversation(self, **kwargs: object):
        self.llamadas.append(kwargs)
        return ConversationResumeResult(
            outcome=self.outcome,
            conversation_id="uuid-conv",
            commercial_case_id="uuid-case",
            resume_event_id="uuid-event",
        )


def _settings_resume(quiet: int = 28_800) -> SimpleNamespace:
    return SimpleNamespace(
        chatwoot_inbox_id=9,
        conversation_resume_quiet_seconds=quiet,
        conversation_resume_max=3,
    )


def _correr(cliente, supabase, settings=None) -> bool:
    return asyncio.run(
        _resume_paused_conversation(
            control_client=cliente,
            supabase=supabase,
            settings=settings or _settings_resume(),
            conversation_id=124,
            message_id=8899,
        )
    )


def test_resume_lifts_both_layers_when_the_team_went_quiet(monkeypatch) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA)
    cliente = _ClienteFalso(mensajes=_mensajes_con_humano(40_000))
    supabase = _SupabaseFalso()

    assert _correr(cliente, supabase) is True
    assert cliente.etiqueta_sacada is True
    assert supabase.llamadas[0]["quiet_seconds"] == 40_000
    assert supabase.llamadas[0]["command_key"] == "resume:124:8899"
    assert supabase.llamadas[0]["reason_code"] == "inbound_after_quiet_period"


def test_resume_waits_while_the_team_is_still_answering(monkeypatch) -> None:
    # Mariana escribio hace dos horas: el agente no se mete en el medio.
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA)
    cliente = _ClienteFalso(mensajes=_mensajes_con_humano(7_200))
    supabase = _SupabaseFalso()

    assert _correr(cliente, supabase) is False
    assert supabase.llamadas == []
    assert cliente.etiqueta_sacada is False


def test_resume_proceeds_when_no_person_ever_wrote(monkeypatch) -> None:
    # Derivada y nunca atendida: no hay silencio que medir, y el agente es
    # mejor que el vacio.
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA)
    supabase = _SupabaseFalso()
    assert _correr(_ClienteFalso(mensajes=[]), supabase) is True
    assert supabase.llamadas[0]["quiet_seconds"] is None


def test_resume_respects_the_opt_out_label(monkeypatch) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA)
    cliente = _ClienteFalso(
        labels=("automation_paused", "automation_opted_out"),
        mensajes=_mensajes_con_humano(90_000),
    )
    supabase = _SupabaseFalso()
    assert _correr(cliente, supabase) is False
    assert supabase.llamadas == []


def test_resume_skips_an_assigned_or_closed_conversation(monkeypatch) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA)
    for cliente in (
        _ClienteFalso(assignee=True, mensajes=_mensajes_con_humano(90_000)),
        _ClienteFalso(status="resolved", mensajes=_mensajes_con_humano(90_000)),
        _ClienteFalso(can_reply=False, mensajes=_mensajes_con_humano(90_000)),
        _ClienteFalso(labels=(), mensajes=_mensajes_con_humano(90_000)),
    ):
        supabase = _SupabaseFalso()
        assert _correr(cliente, supabase) is False
        assert supabase.llamadas == []


def test_resume_reports_false_when_the_durable_layer_refuses(monkeypatch) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA)
    for outcome in ("blocked_contact", "blocked_pending_handoff",
                    "blocked_resume_limit", "not_found"):
        cliente = _ClienteFalso(mensajes=_mensajes_con_humano(90_000))
        assert _correr(cliente, _SupabaseFalso(outcome)) is False
        assert cliente.etiqueta_sacada is False


def test_resume_does_not_claim_success_if_the_label_survives(monkeypatch) -> None:
    # Sin sacar la etiqueta, la guarda pre-envio bloquea igual: no se reintenta
    # la admision para no fabricar una respuesta que nunca sale.
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA)

    class _ClienteQueFalla(_ClienteFalso):
        async def clear_conversation_label(self, **_: object) -> bool:
            raise ChatwootProtocolError("macro_label_not_cleared")

    cliente = _ClienteQueFalla(mensajes=_mensajes_con_humano(90_000))
    assert _correr(cliente, _SupabaseFalso()) is False


def test_resume_fails_closed_when_supabase_errors(monkeypatch) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA)

    class _SupabaseQueFalla(_SupabaseFalso):
        async def resume_paused_conversation(self, **_: object):
            raise SupabaseError("conversation_resume_failed: HTTP 500")

    cliente = _ClienteFalso(mensajes=_mensajes_con_humano(90_000))
    assert _correr(cliente, _SupabaseQueFalla()) is False
    assert cliente.etiqueta_sacada is False


def test_history_hides_team_messages_unless_resume_is_enabled() -> None:
    historia: list[dict[str, object]] = [
        {
            "message_type": 0,
            "private": False,
            "content": "Envíame el enlace",
            "sender": {"type": "contact", "id": 1},
            "created_at": 10,
            "id": 1,
        },
        {
            "message_type": 1,
            "private": False,
            "content": "aquí tienes el enlace https://pay.hotmart.com/X",
            "sender": {"type": "user", "id": 4},
            "created_at": 20,
            "id": 2,
        },
    ]

    sin_flag = _normalize_chatwoot_history(historia, agent_bot_id=1)
    assert [m["actor"] for m in sin_flag] == ["prospect"]

    con_flag = _normalize_chatwoot_history(
        historia, agent_bot_id=1, include_team_messages=True
    )
    assert [m["actor"] for m in con_flag] == ["prospect", "human_agent"]
    assert "pay.hotmart.com" in con_flag[1]["text"]


# ---------------------------------------------------------------------------
# Barredor de reactivacion fuera de la ventana de 24 h


def test_reactivation_settings_default_to_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_from_env(monkeypatch)
    assert settings.conversation_reactivation_enabled is False
    assert settings.conversation_reactivation_template_name is None
    assert settings.conversation_reactivation_interval_seconds == 900.0
    assert settings.conversation_reactivation_min_age_seconds == 86_400
    assert settings.conversation_reactivation_max_age_seconds == 2_592_000
    # El limite por default es uno: a quien recibio la plantilla y no contesto
    # no se le insiste.
    assert settings.conversation_reactivation_max == 1


def test_reactivation_settings_are_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONVERSATION_REACTIVATION_ENABLED", "true")
    monkeypatch.setenv(
        "WABA_REACTIVATION_TEMPLATE_NAME", "johanna_reactivacion_01"
    )
    monkeypatch.setenv("WABA_REACTIVATION_TEMPLATE_LANGUAGE", "es_EC")
    monkeypatch.setenv("CONVERSATION_REACTIVATION_INTERVAL_SECONDS", "600")
    monkeypatch.setenv("CONVERSATION_REACTIVATION_MAX", "2")
    settings = _settings_from_env(monkeypatch)
    assert settings.conversation_reactivation_enabled is True
    assert (
        settings.conversation_reactivation_template_name
        == "johanna_reactivacion_01"
    )
    assert settings.conversation_reactivation_template_language == "es_EC"
    assert settings.conversation_reactivation_interval_seconds == 600.0
    assert settings.conversation_reactivation_max == 2


def _reactivation_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "conversation_reactivation_enabled": True,
        "conversation_reactivation_template_name": "johanna_reactivacion_01",
        "chatwoot_account_id": 1,
        "chatwoot_inbox_id": 9,
        "chatwoot_agent_bot_access_token": "agent-bot-token",
        "agent_bot_id": 1,
        "allowed_jid": "12025550123@s.whatsapp.net",
    }
    values.update(overrides)
    return _settings(**values)


def test_enabling_reactivation_without_a_template_refuses_to_start() -> None:
    # Mandar una plantilla es un efecto externo irreversible: sin plantilla
    # declarada el bridge no arranca, en vez de arrancar a medias.
    with pytest.raises(ValueError) as error:
        create_app(
            _reactivation_settings(
                conversation_reactivation_template_name=None
            ),
            supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
        )
    assert "WABA_REACTIVATION_" in str(error.value)


def test_enabling_reactivation_without_the_agent_bot_refuses_to_start() -> None:
    with pytest.raises(ValueError) as error:
        create_app(
            _reactivation_settings(chatwoot_agent_bot_access_token=None),
            supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
        )
    assert "WABA_REACTIVATION_" in str(error.value)


def test_enabling_reactivation_without_a_bounded_sender_refuses_to_start() -> None:
    with pytest.raises(ValueError) as error:
        create_app(
            _reactivation_settings(
                allowed_jid=None,
                chatwoot_scoped_inbound_senders_enabled=False,
            ),
            supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
        )
    assert "WABA_REACTIVATION_" in str(error.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"conversation_reactivation_interval_seconds": 0.0},
        {"conversation_reactivation_max": 0},
        {"conversation_reactivation_max_sends_per_scan": 0},
        {"conversation_reactivation_max_pages": 0},
        {"conversation_reactivation_max_pages": 21},
        {
            "conversation_reactivation_min_age_seconds": 100,
            "conversation_reactivation_max_age_seconds": 10,
        },
    ],
)
def test_invalid_reactivation_configuration_refuses_to_start(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError) as error:
        create_app(
            _reactivation_settings(**overrides),
            supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
        )
    assert "conversation reactivation configuration" in str(error.value)


def test_the_reactivation_configuration_is_validated_even_when_disabled() -> None:
    # Un valor invalido no se descubre recien el dia que alguien prende el flag.
    with pytest.raises(ValueError):
        create_app(
            _settings(
                conversation_reactivation_enabled=False,
                conversation_reactivation_max_pages=99,
            ),
            supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
        )


def test_the_resume_trigger_is_not_gated_on_a_blocked_admission() -> None:
    """La etiqueta de Chatwoot pausa aunque la admision durable pase.

    Las dos capas de la pausa se escriben por caminos distintos: la etiqueta
    `automation_paused` la pone cualquier saliente de un `user` de Chatwoot,
    y `human_takeover` solo lo pone una derivacion durable. Medido el
    2026-09-23 sobre el inbox 9: de 27 conversaciones etiquetadas, 8 no tenian
    `human_takeover` (114, 126 y 133 entre las abiertas). Si el disparador de
    reanudacion se condiciona a que la admision haya dado 'blocked', esas ocho
    nunca se despausan: la admision pasa, el agente razona, y recien la guarda
    de pre-envio lo frena por la etiqueta que nadie saco.

    Se verifica sobre el AST y no sobre el texto porque lo que importa es la
    forma de la condicion, no como este escrita.
    """
    import ast
    import inspect

    import bridge.app as app_module

    tree = ast.parse(inspect.getsource(app_module))
    parents: dict[ast.AST, ast.AST] = {}
    calls: list[ast.AST] = []
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_resume_paused_conversation"
        ):
            calls.append(node)
    assert calls, "no se encontro el disparador de reanudacion"

    for call in calls:
        gates: list[str] = []
        current: ast.AST | None = call
        while current is not None:
            parent = parents.get(current)
            if isinstance(parent, ast.If) and (
                current is parent.test or current in parent.body
            ):
                gates.append(ast.unparse(parent.test))
            current = parent
        assert any("conversation_resume_enabled" in gate for gate in gates), gates
        for gate in gates:
            # `chatwoot_cut_b_admission_enabled` es un gate legitimo del feature
            # entero; lo prohibido es condicionar el disparador al RESULTADO de
            # la admision de esta conversacion.
            assert "admission.outcome" not in gate, gate


def test_scoped_senders_do_not_narrow_reactivation_to_one_jid() -> None:
    """Con remitentes acotados por scope, el limite no es un JID unico.

    `ALLOWED_WHATSAPP_JID` es un numero de prueba. Cuando
    `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED` esta activo, el limite real son
    las barreras de opt-out, pausa y handoff, y el monitor de conversaciones
    estancadas ya lo respeta con `allow_any_scoped_sender`. Restringir igual al
    JID deja afuera al inbox entero: medido el 2026-09-23 a las 22:40 UTC, el
    barredor salteo las 25 conversaciones abiertas del inbox 9 con
    `target_not_allowed`, y `/ready` publico `healthy` porque saltear a todos
    no es un error.

    Se verifica sobre el AST: lo que importa es que la decision dependa del
    flag, no como este escrita.
    """
    import ast
    import inspect

    import bridge.app as app_module

    tree = ast.parse(inspect.getsource(app_module))
    valores: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        nombre = getattr(func, "id", None) or getattr(func, "attr", None)
        if nombre != "ConversationReactivationSweeper":
            continue
        for keyword in node.keywords:
            if keyword.arg == "allowed_phone":
                valores.append(ast.unparse(keyword.value))
    assert valores, "el barredor no se construye con allowed_phone"
    for valor in valores:
        assert "chatwoot_scoped_inbound_senders_enabled" in valor, valor


def test_readiness_publishes_why_the_reactivation_scan_skipped() -> None:
    """Un barrido sin envios tiene que decir por que.

    Sin esto, un barredor que saltea el inbox entero y uno que no tiene a nadie
    a quien escribir publican exactamente lo mismo.
    """
    import ast
    import inspect

    import bridge.app as app_module

    fuente = inspect.getsource(app_module)
    assert "conversation_reactivation_last_scan" in fuente
    tree = ast.parse(fuente)
    encontrado = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for clave, valor in zip(node.keys, node.values):
                if (
                    isinstance(clave, ast.Constant)
                    and clave.value == "conversation_reactivation_last_scan"
                ):
                    assert "last_scan_summary" in ast.unparse(valor)
                    encontrado = True
    assert encontrado, "la clave no se publica desde el resumen del barredor"


def test_the_resume_trigger_survives_a_real_inbound_with_production_flags(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """El disparador de reanudacion corre sobre un mensaje real sin romperse.

    El 2026-09-23 entre las 23:11 y las 23:48 UTC entraron cinco mensajes al
    inbox 9 (conversaciones 126, 143, 158 y 63) y el bridge fallo los cinco
    con `chatwoot_work_failed error_type=UnboundLocalError` (28 intentos).
    El disparador pasaba `message_id=message_id` a
    `_resume_paused_conversation`, pero en el camino normal (sin reset y sin
    planificacion de descuento) ninguna rama anterior asignaba `message_id`;
    como la funcion lo asigna mas abajo, Python lo trata como local y explota
    al leerlo. Ocho tests probaban `_resume_paused_conversation` aislada y
    uno mas verificaba la forma del disparador sobre el AST: ninguno
    ejecutaba `process_chatwoot_work` con `CONVERSATION_RESUME_ENABLED=true`,
    por eso la suite estaba verde mientras ningun lead recibia respuesta.

    Este test usa el webhook del mensaje 2233 (conversacion 158: un lead
    nuevo, sin pausa, que pidio el enlace) con los flags que definian el
    camino en produccion (admision Cut B activa, planificacion de descuento
    apagada, reanudacion activa) y exige dos cosas: que el worker no registre
    ningun fallo, y que el disparador haya llegado a consultar la
    conversacion en Chatwoot.
    """
    import logging
    import time

    from bridge.chatwoot import ChatwootClient

    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "chatwoot_message_created_inbox_9_conv_158_20260923.json"
        ).read_text(encoding="utf-8")
    )
    webhook = fixture["webhook"]
    conversation = fixture["conversation"]
    messages = fixture["messages"]

    chatwoot_requests: list[httpx.Request] = []

    def chatwoot_handler(request: httpx.Request) -> httpx.Response:
        chatwoot_requests.append(request)
        path = request.url.path
        if path == "/api/v1/accounts/1/conversations/158":
            return httpx.Response(200, json=conversation)
        if path == "/api/v1/accounts/1/conversations/158/messages":
            if request.url.params.get("before") is not None:
                return httpx.Response(200, json={"meta": {}, "payload": []})
            return httpx.Response(200, json={"meta": {}, "payload": messages})
        return httpx.Response(404, json={"error": "unexpected"})

    control_client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        resume_macro_id=3,
        transport=httpx.MockTransport(chatwoot_handler),
    )

    class _AdmittingSupabase:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def admit_inbound_commercial_case(
            self, **kwargs: object
        ) -> object:
            self.calls.append(kwargs)
            return SimpleNamespace(
                outcome="created",
                commercial_case_id="case-158",
                contact_id="contact-158",
                channel_identity_id="identity-158",
                conversation_id="conversation-158",
                automation_status="draft_only",
            )

    supabase = _AdmittingSupabase()
    app = create_app(
        Settings(
            webhook_secret="webhook-secret",
            allowed_jid="573000000158@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            chatwoot_account_id=1,
            chatwoot_inbox_id=9,
            chatwoot_cut_b_admission_enabled=True,
            chatwoot_cut_b_scope_key="libre-de-ansiedad-inbound",
            chatwoot_cut_b_scope_version=1,
            conversation_resume_enabled=True,
            chatwoot_resume_macro_id=3,
        ),
        chatwoot_client=control_client,
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    raw_body = json.dumps(
        webhook, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = "sha256=" + hmac.new(
        b"webhook-secret",
        timestamp.encode("ascii") + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()

    async def exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/webhooks/chatwoot",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Chatwoot-Signature": signature,
                    "X-Chatwoot-Timestamp": timestamp,
                    "X-Chatwoot-Delivery": "conv-158-msg-2233",
                },
            )
        await app.state.chatwoot_worker.run_once()
        return response

    caplog.set_level(logging.WARNING, logger="bridge.chatwoot_inbox")
    response = asyncio.run(exercise())

    assert response.status_code == 202
    fallos = [m for m in caplog.messages if "chatwoot_work_failed" in m]
    assert fallos == [], fallos
    assert [c["external_conversation_id"] for c in supabase.calls] == [158]
    assert any(
        r.url.path == "/api/v1/accounts/1/conversations/158"
        for r in chatwoot_requests
    ), "el disparador de reanudacion no llego a consultar la conversacion"
