"""El entrante de WhatsApp cuando la base conoce al movil con la otra forma.

Con manifiesto, quien dejo el formulario queda guardado como 52 + 10 digitos
(o 54 + 10 en Argentina) y escribe desde 521 + 10 (549 + 10), que es su wa_id.
El bridge resuelve el ``external_user_id`` entrante contra las identidades
activas del inbox antes de hablar con la base: si hay exactamente una y no es
la textual, usa la guardada. Asi un «No mas mensajes» frena al contacto que ya
existe en vez de abrir otro. Lo que se valida contra Chatwoot sigue siendo el
wa_id textual del webhook.

Datos:

* ``chatwoot_message_created_audio_inbox_9_conv_200_20260928.json``: el webhook
  ``message_created`` real de la conversacion 200 del inbox 9 (cuenta 1), de
  una persona mexicana: su ``source_id`` es 521 + 10 digitos (saneados). Es un
  audio, con ``content`` nulo.
* ``tests/fixtures/instances/att1/instancia.toml``: el manifiesto de ATT1. Sus
  ids de Chatwoot (cuenta 2, inbox 11) se cambian por los de la captura
  (cuenta 1, inbox 9), porque el modo scoped exige que coincidan. La captura
  no se toca.
* Para el opt-out hace falta un texto y la captura es un audio. En esos tests
  se reemplaza SOLO ``content`` (en el webhook y en el mismo mensaje del
  historial) por una frase de baja; no hay captura de un «No mas mensajes» ni
  de la pulsacion del boton QUICK_REPLY (deuda anotada en el commit).
* Las filas de ``channel_identities`` son la forma con que PostgREST devuelve
  la tabla (no son un payload externo).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from bridge.app import (
    ChatwootInboundAdmissionRejectedError,
    Settings,
    create_app,
)
from bridge.chatwoot_inbox import RetryableChatwootWorkError
from bridge.commercial_knowledge import CommercialKnowledge
from bridge.instance_manifest import InstanceManifest
from bridge.supabase import (
    InboundOptOutResult,
    SupabaseClient,
    SupabaseError,
    SupabasePermanentError,
    WhatsAppIdentityMatch,
)
from test_audio_transcription import _webhook_200
from test_webhook import (
    StubChatwootClient,
    StubInboundCommercialSupabase,
    StubPaymentLinkSupabase,
    StubShadowProcessor,
    _post,
    _signed_headers,
)

ATT1 = Path(__file__).parent / "fixtures" / "instances" / "att1"
SECRET = "webhook-secret"
WA_ID = "5210000000200"  # el source_id de la captura: 521 + 10 digitos
FORM_ID = "520000000200"  # el mismo movil como lo guarda el formulario
WA_JID = f"{WA_ID}@s.whatsapp.net"
OPT_OUT_TEXT = "No más mensajes"


class _Supabase(StubInboundCommercialSupabase):
    """La base: identidades activas del inbox, admision y opt-out durable."""

    def __init__(
        self,
        *,
        identities: tuple[str, ...] = (),
        stopped_user_ids: tuple[str, ...] = (),
        lookup_error: bool = False,
    ) -> None:
        super().__init__()
        self.identities = identities
        self.stopped_user_ids = stopped_user_ids
        self.lookup_error = lookup_error
        self.identity_lookups: list[dict[str, object]] = []
        self.stop_checks: list[str] = []
        self.opt_outs: list[dict[str, object]] = []
        self.reconciliations: list[dict[str, object]] = []

    async def find_active_whatsapp_identities(
        self, **kwargs: object
    ) -> list[WhatsAppIdentityMatch]:
        self.identity_lookups.append(kwargs)
        if self.lookup_error:
            raise SupabaseError("find_active_whatsapp_identities_failed: HTTP 503")
        return [
            WhatsAppIdentityMatch(
                channel_identity_id=f"identity-{external_user_id}",
                contact_id=f"contact-{external_user_id}",
                external_user_id=external_user_id,
            )
            for external_user_id in self.identities
        ]

    async def has_chatwoot_opt_out_stop(self, **kwargs: object) -> bool:
        external_user_id = str(kwargs["external_user_id"])
        self.stop_checks.append(external_user_id)
        return external_user_id in self.stopped_user_ids

    async def apply_chatwoot_inbound_opt_out(
        self, **kwargs: object
    ) -> InboundOptOutResult:
        self.opt_outs.append(kwargs)
        return InboundOptOutResult(
            outcome="applied",
            opt_out_event_id="opt-out-event-200",
            contact_id="contact-form",
            affected_cases=1,
            affected_actions=1,
            affected_attempts=0,
        )

    async def reconcile_chatwoot_opt_out_stop(
        self, **kwargs: object
    ) -> InboundOptOutResult:
        self.reconciliations.append(kwargs)
        return InboundOptOutResult(
            outcome="already_applied",
            opt_out_event_id="opt-out-event-200",
            contact_id="contact-form",
            affected_cases=0,
            affected_actions=0,
            affected_attempts=0,
        )


def _manifest_settings(tmp_path: Path) -> Settings:
    instance = tmp_path / "instancia"
    shutil.copytree(ATT1, instance)
    knowledge_path = instance / "conocimiento" / "knowledge-v1.toml"
    knowledge_path.write_text(
        knowledge_path.read_text(encoding="utf-8").replace(
            'estado = "borrador"',
            'estado = "aprobado"\naprobado_por = "test"\naprobado_el = 2026-09-28',
        ),
        encoding="utf-8",
    )
    manifest = InstanceManifest.from_toml_file(instance / "instancia.toml")
    manifest = replace(
        manifest,
        flows={**manifest.flows, "inbound": True},
        chatwoot_account_id=1,
        chatwoot_inbox_id=9,
    )
    config = manifest.to_commercial_ally_config()
    return Settings(
        webhook_secret=SECRET,
        allowed_jid=None,
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        commercial_ally_config=config,
        commercial_ally_manifest_path=instance / "instancia.toml",
        instance_manifest=manifest,
        hermes_model_name=manifest.agent_model_name,
        commercial_knowledge=CommercialKnowledge.from_toml_file(knowledge_path),
        agent_bot_id=1,
        chatwoot_account_id=1,
        chatwoot_inbox_id=9,
        chatwoot_scoped_inbound_senders_enabled=True,
        chatwoot_cut_b_admission_enabled=True,
        chatwoot_cut_b_scope_key=config.inbound_scope_key,
        chatwoot_cut_b_scope_version=config.inbound_scope_version,
        chatwoot_cut_b_agent_enabled=True,
        automated_replies_enabled=True,
        chatwoot_durable_opt_out_enabled=True,
        chatwoot_opt_out_macro_id=5,
        opt_out_projection_worker_id="opt-out-projection-test",
        chatwoot_human_pause_enabled=True,
        human_handoff_admission_enabled=True,
        human_handoff_projection_enabled=True,
        handoff_projection_policy_key="att1-derivacion",
        handoff_projection_policy_version=1,
        human_handoff_projection_worker_id="handoff-projection-test",
    )


def _johanna_settings(tmp_path: Path) -> Settings:
    # Johanna hoy: sin manifiesto, con su remitente fijo (el mismo armado de
    # tests/test_app_audio_transcription.py).
    return Settings(
        webhook_secret=SECRET,
        allowed_jid=WA_JID,
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        agent_bot_id=1,
        chatwoot_account_id=1,
        chatwoot_inbox_id=9,
        chatwoot_cut_b_admission_enabled=True,
        chatwoot_cut_b_scope_key="libre-de-ansiedad-inbound",
        chatwoot_cut_b_scope_version=2,
        chatwoot_cut_b_agent_enabled=True,
        automated_replies_enabled=True,
    )


def _text_webhook(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """La captura con ``content`` reemplazado, y el mismo mensaje en el historial."""
    webhook = copy.deepcopy(_webhook_200())
    webhook["content"] = text
    webhook["attachments"] = []
    message = copy.deepcopy(webhook["conversation"]["messages"][0])
    message["content"] = text
    message["attachments"] = []
    webhook["conversation"]["messages"] = [copy.deepcopy(message)]
    return webhook, [message]


def _process(
    tmp_path: Path,
    supabase: _Supabase,
    *,
    settings: Settings | None = None,
    webhook: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    proposal: dict[str, object] | None = None,
) -> tuple[StubChatwootClient, StubShadowProcessor, Any]:
    shadow = StubShadowProcessor(
        proposal or {"decision": "reply", "reply": "Te leemos."}
    )
    chatwoot = StubChatwootClient(
        messages=history
        if history is not None
        else [copy.deepcopy(_webhook_200()["conversation"]["messages"][0])]
    )
    app = create_app(
        settings or _manifest_settings(tmp_path),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    raw_body = json.dumps(
        webhook if webhook is not None else _webhook_200(), separators=(",", ":")
    ).encode("utf-8")
    response = _post(
        app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery="inbound-200")
    )
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    return chatwoot, shadow, app


# ------------------------------------------------------------- la admision


def test_the_captured_wa_id_is_the_whatsapp_form_of_a_mexican_mobile() -> None:
    assert _webhook_200()["conversation"]["contact_inbox"]["source_id"] == WA_ID
    assert len(WA_ID) == 13 and WA_ID.startswith("521")
    assert FORM_ID == "52" + WA_ID[3:]


def test_inbound_uses_the_identity_the_form_already_stored(tmp_path: Path) -> None:
    supabase = _Supabase(identities=(FORM_ID,))

    _process(tmp_path, supabase)

    assert supabase.identity_lookups[0] == {
        "chatwoot_account_id": 1,
        "chatwoot_inbox_id": 9,
        "external_user_ids": (FORM_ID, WA_ID),
    }
    [admission] = supabase.admission_calls
    assert admission["external_user_id"] == FORM_ID
    assert admission["external_conversation_id"] == 200


def test_inbound_without_a_stored_identity_keeps_the_textual_wa_id(
    tmp_path: Path,
) -> None:
    supabase = _Supabase(identities=())

    _process(tmp_path, supabase)

    assert supabase.identity_lookups
    [admission] = supabase.admission_calls
    assert admission["external_user_id"] == WA_ID


def test_inbound_with_its_own_identity_keeps_it(tmp_path: Path) -> None:
    supabase = _Supabase(identities=(WA_ID,))

    _process(tmp_path, supabase)

    [admission] = supabase.admission_calls
    assert admission["external_user_id"] == WA_ID


def test_inbound_with_both_identities_uses_the_textual_one_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # La misma persona con dos identidades (dos contactos): no se elige por
    # ella. Queda la textual y un aviso con ids de Chatwoot y region.
    supabase = _Supabase(identities=(FORM_ID, WA_ID))

    with caplog.at_level(logging.WARNING):
        _process(tmp_path, supabase)

    [admission] = supabase.admission_calls
    assert admission["external_user_id"] == WA_ID
    [warning] = [
        record.getMessage()
        for record in caplog.records
        if "chatwoot_inbound_identity_duplicated" in record.getMessage()
    ]
    assert "account=1" in warning and "inbox=9" in warning
    assert "conversation=200" in warning and "region=MX" in warning
    assert "identities=2" in warning
    # Nunca el numero, en ninguna de sus formas.
    assert WA_ID not in caplog.text and FORM_ID not in caplog.text
    assert WA_ID[3:] not in caplog.text


def test_a_failed_identity_lookup_keeps_the_work_for_a_retry(tmp_path: Path) -> None:
    supabase = _Supabase(lookup_error=True)

    _, _, app = _process(tmp_path, supabase)

    # No se admite con una identidad adivinada: el trabajo queda para reintentar.
    assert supabase.admission_calls == []
    [item] = app.state.chatwoot_inbox.admitted_items(include_deferred=True)
    assert item.delivery_id == "inbound-200"


# ---------------------------------------------------------------- el opt-out


def test_an_opt_out_from_the_whatsapp_form_stops_the_identity_of_the_form(
    tmp_path: Path,
) -> None:
    supabase = _Supabase(identities=(FORM_ID,))
    webhook, history = _text_webhook(OPT_OUT_TEXT)

    chatwoot, shadow, _ = _process(
        tmp_path, supabase, webhook=webhook, history=history
    )

    [opt_out] = supabase.opt_outs
    assert opt_out["external_user_id"] == FORM_ID
    assert opt_out["chatwoot_conversation_id"] == 200
    assert opt_out["chatwoot_message_id"] == 2472
    assert opt_out["rule_key"] == "stop_receiving_messages"
    # El agente no corre ni contesta.
    assert shadow.calls == []
    assert chatwoot.reply_calls == []
    # Contra Chatwoot se valida el wa_id textual del webhook, no el resuelto.
    assert {call["expected_jid"] for call in chatwoot.authority_calls} == {WA_JID}


def test_the_stop_check_looks_at_every_form_of_the_phone(tmp_path: Path) -> None:
    supabase = _Supabase(identities=(FORM_ID,))
    webhook, history = _text_webhook("Hola, ¿siguen ahí?")

    chatwoot, shadow, _ = _process(
        tmp_path, supabase, webhook=webhook, history=history
    )

    # El resuelto primero y despues la otra forma: ninguna tiene un stop.
    assert supabase.stop_checks == [FORM_ID, WA_ID]
    assert supabase.reconciliations == []
    assert len(shadow.calls) == 1
    # La admision y su reautorizacion usan la identidad guardada.
    assert {call["external_user_id"] for call in supabase.admission_calls} == {FORM_ID}
    [reply] = chatwoot.reply_calls
    assert reply["expected_jid"] == WA_JID


def test_an_opt_out_stored_under_the_other_form_still_stops_the_reply(
    tmp_path: Path,
) -> None:
    # La persona se dio de baja cuando la base todavia no la conocia: el
    # opt-out quedo bajo el wa_id textual (521…). Despues el formulario creo su
    # identidad 52…, que es la que hoy resuelve el entrante. El stop vale igual.
    supabase = _Supabase(identities=(FORM_ID,), stopped_user_ids=(WA_ID,))
    webhook, history = _text_webhook("Hola, ¿siguen ahí?")

    chatwoot, shadow, _ = _process(
        tmp_path, supabase, webhook=webhook, history=history
    )

    assert supabase.stop_checks == [FORM_ID, WA_ID]
    [reconciliation] = supabase.reconciliations
    assert reconciliation["external_user_id"] == WA_ID
    assert shadow.calls == []
    assert chatwoot.reply_calls == []


def test_the_reset_command_respects_a_stop_under_any_form(tmp_path: Path) -> None:
    # "/nuevo" tiene su propio chequeo de stop antes de confirmar: tambien mira
    # las dos formas.
    stopped = _Supabase(identities=(FORM_ID,), stopped_user_ids=(WA_ID,))
    webhook, history = _text_webhook("/nuevo")

    chatwoot, _, _ = _process(tmp_path, stopped, webhook=webhook, history=history)

    assert stopped.stop_checks == [FORM_ID, WA_ID]
    assert [call["external_user_id"] for call in stopped.reconciliations] == [WA_ID]
    assert chatwoot.reply_calls == []

    free = _Supabase(identities=(FORM_ID,))
    chatwoot, _, _ = _process(
        tmp_path / "sin-stop", free, webhook=webhook, history=history
    )

    assert free.stop_checks == [FORM_ID, WA_ID]
    [reply] = chatwoot.reply_calls
    assert reply["content"] == "Memoria eliminada."
    assert reply["expected_jid"] == WA_JID


# ------------------------------------------------------------------- Johanna


class _JohannaSupabase(_Supabase):
    async def find_active_whatsapp_identities(self, **_: object) -> list[Any]:
        raise AssertionError("a runtime without a manifest never resolves forms")


def test_without_a_manifest_the_wa_id_is_used_as_it_arrives(tmp_path: Path) -> None:
    supabase = _JohannaSupabase(identities=(FORM_ID,))
    webhook, history = _text_webhook("Hola, ¿siguen ahí?")

    chatwoot, shadow, _ = _process(
        tmp_path,
        supabase,
        settings=_johanna_settings(tmp_path),
        webhook=webhook,
        history=history,
    )

    # La admision y su reautorizacion antes de responder, las dos textuales.
    assert supabase.admission_calls
    assert {call["external_user_id"] for call in supabase.admission_calls} == {WA_ID}
    # Un solo chequeo de stop, con el id textual: lo de siempre.
    assert supabase.stop_checks == [WA_ID]
    assert len(chatwoot.reply_calls) == 1


def test_the_projections_accept_the_other_form_only_with_a_manifest(
    tmp_path: Path,
) -> None:
    # El opt-out y la derivacion quedan guardados con la identidad resuelta
    # (52…) y la conversacion de Chatwoot es del wa_id (521…): con manifiesto
    # las proyecciones aceptan la otra forma; sin manifiesto validan exacto.
    manifest_app = create_app(
        _manifest_settings(tmp_path),
        chatwoot_client=StubChatwootClient(),
        shadow_processor=StubShadowProcessor(),
        supabase_client=_Supabase(),  # type: ignore[arg-type]
    )
    johanna_app = create_app(
        replace(
            _johanna_settings(tmp_path),
            chatwoot_durable_opt_out_enabled=True,
            chatwoot_opt_out_macro_id=5,
            opt_out_projection_worker_id="opt-out-projection-test",
            human_handoff_admission_enabled=True,
            human_handoff_projection_enabled=True,
            handoff_projection_policy_key="lancemos-inbound-handoff",
            handoff_projection_policy_version=1,
            human_handoff_projection_worker_id="handoff-projection-test",
        ),
        chatwoot_client=StubChatwootClient(),
        shadow_processor=StubShadowProcessor(),
        supabase_client=_JohannaSupabase(),  # type: ignore[arg-type]
    )

    for worker in ("opt_out_projection_worker", "human_handoff_projection_worker"):
        assert getattr(manifest_app.state, worker)._whatsapp_equivalence_enabled is True
        assert getattr(johanna_app.state, worker)._whatsapp_equivalence_enabled is False


# -------------------------------------------- la lectura de channel_identities


def _identity_row(external_user_id: str, *, inbox_id: object) -> dict[str, Any]:
    return {
        "id": f"identity-{external_user_id}",
        "contact_id": f"contact-{external_user_id}",
        "external_user_id": external_user_id,
        "metadata": {} if inbox_id is None else {"inbox_id": inbox_id},
    }


def _identities(rows: list[dict[str, Any]], seen: list[httpx.Request]) -> SupabaseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=rows, request=request)

    return SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="test-key",
        transport=httpx.MockTransport(handler),
    )


def test_identity_lookup_reads_only_active_whatsapp_identities_of_the_account() -> None:
    seen: list[httpx.Request] = []
    client = _identities(
        [
            # La base guarda el inbox como numero o como texto segun quien
            # creo la identidad (el planificador o la admision entrante).
            _identity_row(FORM_ID, inbox_id=9),
            _identity_row(WA_ID, inbox_id="9"),
        ],
        seen,
    )

    matches = asyncio.run(client.find_active_whatsapp_identities(
        chatwoot_account_id=1,
        chatwoot_inbox_id=9,
        external_user_ids=(FORM_ID, WA_ID),
    ))

    [request] = seen
    assert request.method == "GET"
    assert request.url.path == "/rest/v1/channel_identities"
    params = dict(request.url.params)
    assert params["channel"] == "eq.whatsapp"
    assert params["account_id"] == "eq.chatwoot:1"
    assert params["identity_status"] == "eq.active"
    assert params["external_user_id"] == f"in.({FORM_ID},{WA_ID})"
    assert [match.external_user_id for match in matches] == [FORM_ID, WA_ID]
    assert matches[0].contact_id == f"contact-{FORM_ID}"


def test_identity_lookup_leaves_out_identities_not_bound_to_the_inbox() -> None:
    # La misma condicion que aplican el opt-out durable y la admision
    # entrante: metadata ->> 'inbox_id' tiene que ser el inbox.
    client = _identities(
        [
            _identity_row(FORM_ID, inbox_id=24),
            _identity_row(WA_ID, inbox_id=None),
        ],
        [],
    )

    matches = asyncio.run(client.find_active_whatsapp_identities(
        chatwoot_account_id=1,
        chatwoot_inbox_id=9,
        external_user_ids=(FORM_ID, WA_ID),
    ))

    assert matches == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"chatwoot_account_id": 0, "chatwoot_inbox_id": 9, "external_user_ids": (WA_ID,)},
        {"chatwoot_account_id": 1, "chatwoot_inbox_id": True, "external_user_ids": (WA_ID,)},
        {"chatwoot_account_id": 1, "chatwoot_inbox_id": 9, "external_user_ids": ()},
        {
            "chatwoot_account_id": 1,
            "chatwoot_inbox_id": 9,
            "external_user_ids": (WA_ID, "1) or (1"),
        },
    ],
)
def test_identity_lookup_refuses_anything_but_ids_and_digits(
    kwargs: dict[str, Any],
) -> None:
    seen: list[httpx.Request] = []
    client = _identities([], seen)

    with pytest.raises(SupabaseError, match="find_active_whatsapp_identities_invalid_input"):
        asyncio.run(client.find_active_whatsapp_identities(**kwargs))

    assert seen == []


def test_identity_lookup_refuses_a_row_it_did_not_ask_for() -> None:
    client = _identities([_identity_row("5215599999999", inbox_id=9)], [])

    with pytest.raises(SupabaseError):
        asyncio.run(client.find_active_whatsapp_identities(
            chatwoot_account_id=1,
            chatwoot_inbox_id=9,
            external_user_ids=(FORM_ID, WA_ID),
        ))


# ------------------------------------- el opt-out cuando la admision no pasa


class _RejectingAdmission(_Supabase):
    """La base rechaza la admision entrante de esta conversacion.

    Es lo que pasa hoy con quien responde a una plantilla del dispatcher: la
    aceptacion deja la conversacion en ``enabled`` y la admision entrante solo
    acepta una ``draft_only`` (22000 ``inbound_canonical_conversation_conflict``;
    lo fija ``validate_att1_portable_chain.mjs``). El cliente lo levanta como
    un rechazo permanente con ese motivo; ``transient`` es una caida comun.
    """

    def __init__(self, *, transient: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.transient = transient

    async def admit_inbound_commercial_case(self, **kwargs: object) -> Any:
        self.admission_calls.append(kwargs)
        if self.transient:
            raise SupabaseError("inbound_commercial_case_admission_failed: HTTP 503")
        raise SupabasePermanentError(
            "inbound_commercial_case_admission_failed: HTTP 400",
            reason="inbound_canonical_conversation_conflict",
        )


def _pending(app: Any) -> list[dict[str, Any]]:
    return [
        json.loads(item.path.read_text(encoding="utf-8"))
        for item in app.state.chatwoot_inbox.admitted_items(include_deferred=True)
    ]


@pytest.mark.parametrize("transient", [False, True])
def test_an_opt_out_is_recorded_even_when_the_admission_does_not_pass(
    tmp_path: Path, transient: bool
) -> None:
    # La persona que recibio la plantilla aprieta «No mas mensajes». La
    # admision de esa conversacion no pasa, y antes el opt-out (que se detecta
    # despues de admitir) no se registraba nunca.
    supabase = _RejectingAdmission(identities=(FORM_ID,), transient=transient)
    webhook, history = _text_webhook(OPT_OUT_TEXT)

    chatwoot, shadow, app = _process(tmp_path, supabase, webhook=webhook, history=history)

    [opt_out] = supabase.opt_outs
    assert opt_out["external_user_id"] == FORM_ID
    assert opt_out["chatwoot_conversation_id"] == 200
    assert opt_out["chatwoot_message_id"] == 2472
    assert opt_out["rule_key"] == "stop_receiving_messages"
    # El trabajo termina: no queda reintentando.
    assert _pending(app) == []
    # Y el agente no corre ni contesta.
    assert shadow.calls == []
    assert chatwoot.reply_calls == []
    assert {call["expected_jid"] for call in chatwoot.authority_calls} == {WA_JID}


def test_a_stopped_person_is_not_retried_when_the_admission_does_not_pass(
    tmp_path: Path,
) -> None:
    # Ya se habia dado de baja (el opt-out quedo bajo el wa_id textual) y
    # vuelve a escribir en la conversacion de la plantilla: se reconcilia el
    # stop y el trabajo termina.
    supabase = _RejectingAdmission(identities=(FORM_ID,), stopped_user_ids=(WA_ID,))
    webhook, history = _text_webhook("Hola, ¿siguen ahí?")

    chatwoot, shadow, app = _process(tmp_path, supabase, webhook=webhook, history=history)

    assert [call["external_user_id"] for call in supabase.reconciliations] == [WA_ID]
    assert supabase.opt_outs == []
    assert _pending(app) == []
    assert shadow.calls == [] and chatwoot.reply_calls == []


def test_a_rejected_admission_is_not_retried_without_limit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Un mensaje comun sobre una conversacion que la admision rechaza para
    # siempre. Reintentarlo no cambia la respuesta: el motivo queda en el log y
    # el trabajo deja de ser "reintentable sin limite" (termina como failed
    # tras los intentos acotados). Nadie contesta desde aca.
    supabase = _RejectingAdmission(identities=(FORM_ID,))
    webhook, history = _text_webhook("Sí, quiero el enlace")

    with caplog.at_level(logging.WARNING):
        chatwoot, shadow, app = _process(tmp_path, supabase, webhook=webhook, history=history)

    assert supabase.opt_outs == [] and supabase.reconciliations == []
    assert shadow.calls == [] and chatwoot.reply_calls == []
    [item] = _pending(app)
    assert item["attempts"] == 1
    assert item["last_error_type"] == "ChatwootInboundAdmissionRejectedError"
    [warning] = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("chatwoot_cut_b_admission_rejected")
    ]
    assert warning == (
        "chatwoot_cut_b_admission_rejected conversation=200 "
        "reason=inbound_canonical_conversation_conflict"
    )
    assert WA_ID not in caplog.text and FORM_ID not in caplog.text
    # El worker reintenta sin limite solo un RetryableChatwootWorkError; todo
    # otro error tiene los intentos acotados y termina como failed.
    assert not issubclass(
        ChatwootInboundAdmissionRejectedError, RetryableChatwootWorkError
    )


def test_a_transient_admission_failure_is_still_retried(tmp_path: Path) -> None:
    # La base caida no es un rechazo: el trabajo queda para reintentar, sin
    # limite, como siempre.
    supabase = _RejectingAdmission(identities=(FORM_ID,), transient=True)
    webhook, history = _text_webhook("Sí, quiero el enlace")

    _, shadow, app = _process(tmp_path, supabase, webhook=webhook, history=history)

    assert supabase.opt_outs == []
    assert shadow.calls == []
    [item] = _pending(app)
    assert item["last_error_type"] == "RetryableChatwootWorkError"


def test_without_a_manifest_a_failed_admission_behaves_as_before(tmp_path: Path) -> None:
    # Johanna: sin manifiesto no hay opt-out previo a la admision ni corte de
    # los reintentos. Un fallo de la admision, del tipo que sea, reintenta.
    supabase = _RejectingAdmission()
    webhook, history = _text_webhook(OPT_OUT_TEXT)

    chatwoot, shadow, app = _process(
        tmp_path,
        supabase,
        settings=_johanna_settings(tmp_path),
        webhook=webhook,
        history=history,
    )

    assert supabase.identity_lookups == []
    assert supabase.stop_checks == [] and supabase.opt_outs == []
    assert chatwoot.authority_calls == []
    assert shadow.calls == []
    [item] = _pending(app)
    assert item["last_error_type"] == "RetryableChatwootWorkError"


# ------------------------------------------ el enlace de pago del entrante
#
# Quien dejo el formulario con 52... y escribe desde su wa_id 521... pide el
# enlace. La reserva compartida busca la intencion con el mismo id con que
# exige la identidad del caso: con la identidad en 521... y la intencion del
# formulario en 52... el enlace salia con la oferta por defecto, sin el sck
# del formulario, y le llegaba a quien ya habia comprado. Con manifiesto el
# bridge reserva con reserve_portable_checkout_issuance_v2, que busca la
# intencion por las dos formas. El comportamiento de las dos RPC se prueba en
# tests/sql/followup_engine/validate_whatsapp_phone_equivalence.mjs (10).

PAYMENT_LINK_PROPOSAL: dict[str, object] = {
    "decision": "send_payment_link",
    "qualification_status": "in_progress",
    "reason_code": "payment_link_requested",
    "reply": "Claro, aca tenes el enlace:",
    "captured_fields": {},
    "missing_fields": [],
}


class _PaymentLinkSupabase(_Supabase, StubPaymentLinkSupabase):
    """La base del entrante con la emision del enlace (reserva, autorizacion
    y cierre) del stub de tests/test_webhook.py."""


def _payment_link(
    tmp_path: Path, supabase: _PaymentLinkSupabase, settings: Settings
) -> StubChatwootClient:
    webhook, history = _text_webhook("Pasame el link para pagar")
    chatwoot, _, _ = _process(
        tmp_path,
        supabase,
        settings=replace(settings, payment_link_enabled=True),
        webhook=webhook,
        history=history,
        proposal=PAYMENT_LINK_PROPOSAL,
    )
    return chatwoot


def test_with_a_manifest_the_payment_link_looks_for_the_intent_in_every_form(
    tmp_path: Path,
) -> None:
    supabase = _PaymentLinkSupabase(identities=(FORM_ID,))

    chatwoot = _payment_link(tmp_path, supabase, _manifest_settings(tmp_path))

    [reservation] = supabase.candidate_calls
    assert reservation["phone_equivalence"] is True
    assert reservation["external_user_id"] == FORM_ID
    assert reservation["chatwoot_conversation_id"] == 200
    # Autorizar y cerrar no cambian: trabajan por issuance_id.
    [authorization] = supabase.prepare_calls
    assert "phone_equivalence" not in authorization
    assert authorization["external_user_id"] == FORM_ID
    [finalization] = supabase.finalize_calls
    assert "phone_equivalence" not in finalization
    [reply] = chatwoot.reply_calls
    assert "src=hermes" in str(reply["content"])


def test_without_a_manifest_the_payment_link_uses_the_shared_reserve(
    tmp_path: Path,
) -> None:
    supabase = _PaymentLinkSupabase(identities=(FORM_ID,))

    # Johanna con el enlace prendido: el mismo armado que el test del enlace
    # de tests/test_webhook.py (scoped, opt-out durable y derivacion).
    johanna = replace(
        _johanna_settings(tmp_path),
        chatwoot_scoped_inbound_senders_enabled=True,
        chatwoot_durable_opt_out_enabled=True,
        chatwoot_human_pause_enabled=True,
        chatwoot_opt_out_macro_id=2,
        opt_out_projection_worker_id="opt-out-test",
        human_handoff_admission_enabled=True,
        human_handoff_projection_enabled=True,
        handoff_projection_policy_key="lancemos-inbound-handoff",
        handoff_projection_policy_version=1,
        human_handoff_projection_worker_id="handoff-projection-test",
    )
    chatwoot = _payment_link(tmp_path, supabase, johanna)

    # Johanna: la reserva de siempre, con el wa_id textual y sin el flag.
    [reservation] = supabase.candidate_calls
    assert "phone_equivalence" not in reservation
    assert reservation["external_user_id"] == WA_ID
    assert supabase.identity_lookups == []
    assert len(chatwoot.reply_calls) == 1


def _reserving_client(seen: list[httpx.Request]) -> SupabaseClient:
    ulid = "01K3F8QW7N2VYB4M6X9CDPTZRA"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[{
            "outcome": "reserved",
            "issuance_id": "00000000-0000-0000-0000-000000000301",
            "issuance_ulid": ulid,
            "purchase_intent_id": "00000000-0000-0000-0000-000000000302",
            "source_kind": "precheckout_request",
            "checkout_url_final": (
                "https://pay.hotmart.com/D98014973Y?off=2uafw5bg&checkoutMode=10"
                f"&src=hermes&sck=hermes%7Cv1%7C{ulid}"
            ),
            "source_value": "hermes",
            "sck_value": f"hermes|v1|{ulid}",
        }], request=request)

    return SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="test-key",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    ("flag", "path"),
    [
        ({"phone_equivalence": True}, "/rest/v1/rpc/reserve_portable_checkout_issuance_v2"),
        ({"phone_equivalence": False}, "/rest/v1/rpc/reserve_chatwoot_checkout_issuance_v2"),
        ({}, "/rest/v1/rpc/reserve_chatwoot_checkout_issuance_v2"),
    ],
)
def test_the_reserve_rpc_depends_only_on_the_flag(
    flag: dict[str, bool], path: str
) -> None:
    seen: list[httpx.Request] = []
    client = _reserving_client(seen)

    result = asyncio.run(client.reserve_chatwoot_checkout_issuance_v2(
        commercial_case_id="00000000-0000-0000-0000-000000000300",
        external_user_id=FORM_ID,
        chatwoot_account_id=1,
        chatwoot_inbox_id=9,
        chatwoot_conversation_id=200,
        trigger_external_message_id="501",
        issuance_ulid="01K3F8QW7N2VYB4M6X9CDPTZRA",
        now="2026-10-01T12:00:00+00:00",
        **flag,
    ))

    [request] = seen
    assert request.method == "POST"
    assert request.url.path == path
    # Mismo payload y misma validacion de la respuesta en las dos.
    assert json.loads(request.content) == {
        "p_commercial_case_id": "00000000-0000-0000-0000-000000000300",
        "p_external_user_id": FORM_ID,
        "p_chatwoot_account_id": 1,
        "p_chatwoot_inbox_id": 9,
        "p_chatwoot_conversation_id": 200,
        "p_trigger_external_message_id": "501",
        "p_issuance_ulid": "01K3F8QW7N2VYB4M6X9CDPTZRA",
        "p_now": "2026-10-01T12:00:00+00:00",
    }
    assert result.outcome == "reserved"
    assert result.source_kind == "precheckout_request"
