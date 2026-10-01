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
* Los botones de la plantilla salen de ``chatwoot_inbox_11_message_templates_20261001.json``
  y la plantilla saliente en el historial, de la forma del mensaje 2224 de
  ``chatwoot_message_created_inbox_9_conv_158_20260923.json`` (ver la seccion
  H7, abajo).
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
from bridge.approved_templates import parse_approved_template
from bridge.chatwoot_inbox import RetryableChatwootWorkError
from bridge.commercial_knowledge import CommercialKnowledge
from bridge.instance_manifest import InstanceManifest
from bridge.supabase import (
    ConversationResumeResult,
    InboundCommercialCaseAdmissionResult,
    InboundOptOutResult,
    SupabaseClient,
    SupabaseError,
    SupabasePermanentError,
    WhatsAppIdentityMatch,
)
from test_audio_transcription import _webhook_200
from test_app import (
    AHORA_177,
    JID_DEL_LEAD_177,
    NUMERO_DE_PRUEBA,
    _Chatwoot177,
    _cliente_177,
    _fixture_177,
)
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

    La aceptacion de una plantilla del dispatcher deja la conversacion en
    ``enabled`` y la v2 solo acepta una ``draft_only`` (22000
    ``inbound_canonical_conversation_conflict``). Desde H7 la portable adopta
    esa conversacion, pero no siempre: con el contacto dado de baja, otra
    identidad o un paso pendiente no adopta y delega en la v2, que rechaza
    igual (``validate_portable_inbound_template_adoption.mjs``). Aca rechazan
    las dos RPC. El cliente lo levanta como un rechazo permanente con ese
    motivo; ``transient`` es una caida comun.
    """

    def __init__(self, *, transient: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.transient = transient

    async def admit_inbound_commercial_case(self, **kwargs: object) -> Any:
        self.admission_rpcs.append("v2")
        return self._reject(kwargs)

    async def admit_portable_inbound_commercial_case(self, **kwargs: object) -> Any:
        self.admission_rpcs.append("portable")
        return self._reject(kwargs)

    def _reject(self, kwargs: dict[str, object]) -> Any:
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


# ------------------------- la respuesta a una plantilla del piloto (H7)
#
# Quien recibio una plantilla del dispatcher (primer contacto, carrito o pago
# fallido) aprieta un boton y escribe en la conversacion que la aceptacion del
# envio dejo en ``enabled``. La v2 la rechaza (22000
# ``inbound_canonical_conversation_conflict``) y antes el agente no corria. Con
# manifiesto el bridge admite con admit_portable_inbound_commercial_case_v1,
# que adopta esa conversacion y delega en la v2; lo que hace la RPC se prueba
# en tests/sql/followup_engine/validate_portable_inbound_template_adoption.mjs.
# Aca se prueba que el bridge pide la portable en las tres llamadas a la
# admision y que Johanna sigue pidiendo la v2 con los mismos argumentos.
#
# Datos: los botones salen de la captura de las plantillas del inbox 11
# (``chatwoot_inbox_11_message_templates_20261001.json``). Un QUICK_REPLY llega
# a Chatwoot como un mensaje entrante de texto con el texto del boton en
# ``content``: asi esta en la captura del mensaje 2233 de la conversacion 158
# (``chatwoot_message_created_inbox_9_conv_158_20260923.json``, «Envíame el
# enlace» de una plantilla de Johanna). De ATT1 no hay captura de la pulsacion;
# por eso el webhook es la captura de la conversacion 200 con ``content``
# reemplazado, como en los tests del opt-out de arriba.

PILOT_TEMPLATES = (
    "att1_interes_precheckout_01",
    "att1_carrito_abandonado_01",
    "att1_compra_fallida_01",
)
INBOX_11_TEMPLATES = (
    Path(__file__).parent / "fixtures" / "chatwoot_inbox_11_message_templates_20261001.json"
)


def _pilot_buttons() -> tuple[str, ...]:
    """Los botones de las plantillas del piloto, iguales en las tres."""
    captured = json.loads(INBOX_11_TEMPLATES.read_text(encoding="utf-8"))
    buttons = {
        template["name"]: tuple(
            button["text"]
            for component in template["components"]
            if component["type"] == "BUTTONS"
            for button in component["buttons"]
            if button["type"] == "QUICK_REPLY"
        )
        for template in captured["templates"]
        if template["name"] in PILOT_TEMPLATES
    }
    assert set(buttons) == set(PILOT_TEMPLATES)
    [shared] = set(buttons.values())
    return shared


def test_the_pilot_templates_carry_the_three_buttons() -> None:
    # Si una plantilla cambiara sus botones, los tests de abajo dejarian de
    # cubrir lo que el lead realmente aprieta.
    assert sorted(_pilot_buttons()) == sorted(
        ("Envíame el enlace", "Necesito ayuda", "No más mensajes")
    )


class _TemplateConversation(_PaymentLinkSupabase):
    """La conversacion que abrio una plantilla del piloto.

    La v2 la rechaza como hoy (22000 ``inbound_canonical_conversation_conflict``,
    lo fija ``validate_att1_portable_chain.mjs``); la portable la adopta y la
    admite en ``draft_only``.
    """

    async def admit_inbound_commercial_case(self, **kwargs: object) -> Any:
        self.admission_rpcs.append("v2")
        self.admission_calls.append(kwargs)
        raise SupabasePermanentError(
            "inbound_commercial_case_admission_failed: HTTP 400",
            reason="inbound_canonical_conversation_conflict",
        )


HANDOFF_PROPOSAL: dict[str, object] = {
    "decision": "handoff",
    "reply": "Te paso con una persona del equipo.",
}


def _press(
    tmp_path: Path, supabase: _TemplateConversation, button: str, proposal: dict[str, object]
) -> tuple[StubChatwootClient, StubShadowProcessor, Any]:
    webhook, history = _text_webhook(button)
    return _process(
        tmp_path,
        supabase,
        settings=replace(_manifest_settings(tmp_path), payment_link_enabled=True),
        webhook=webhook,
        history=history,
        proposal=proposal,
    )


def _admission_kwargs(external_user_id: str, *, scope_key: str, scope_version: int) -> dict[str, object]:
    return {
        "scope_key": scope_key,
        "scope_version": scope_version,
        "external_conversation_id": 200,
        "external_user_id": external_user_id,
    }


def _manifest_settings_scope() -> tuple[str, int]:
    """El scope entrante de ATT1, el mismo que arma _manifest_settings."""
    manifest = InstanceManifest.from_toml_file(ATT1 / "instancia.toml")
    config = manifest.to_commercial_ally_config()
    return config.inbound_scope_key, config.inbound_scope_version


def _att1_admission() -> dict[str, object]:
    scope_key, scope_version = _manifest_settings_scope()
    return _admission_kwargs(FORM_ID, scope_key=scope_key, scope_version=scope_version)


def test_send_me_the_link_reaches_the_agent_and_the_link_goes_out(
    tmp_path: Path,
) -> None:
    [button] = [b for b in _pilot_buttons() if b == "Envíame el enlace"]
    supabase = _TemplateConversation(identities=(FORM_ID,))

    chatwoot, shadow, app = _press(tmp_path, supabase, button, PAYMENT_LINK_PROPOSAL)

    # El agente ve el boton.
    [(_, context)] = shadow.calls
    assert context["messages"][-1]["text"] == button  # type: ignore[index]
    # La admision, por la portable y con la identidad del formulario. La v2 no
    # se pide nunca.
    assert supabase.admission_rpcs == ["portable"]
    assert supabase.admission_calls == [_att1_admission()]
    # El enlace sale sobre el caso que devolvio la admision.
    [reservation] = supabase.candidate_calls
    assert reservation["commercial_case_id"] == "case-1"
    assert reservation["phone_equivalence"] is True
    [reply] = chatwoot.reply_calls
    assert "src=hermes" in str(reply["content"])
    assert _pending(app) == []


# La plantilla saliente en el historial. De ATT1 no hay captura de un mensaje
# de plantilla en Chatwoot: la forma es la del mensaje 2224 de la conversacion
# 158 del inbox 9 (``chatwoot_message_created_inbox_9_conv_158_20260923.json``),
# la plantilla de primer contacto de Johanna que el bridge mando con su agent
# bot, justo antes del «Envíame el enlace» capturado. El texto es el que arma
# el dispatcher en modo directo: el cuerpo aprobado de la plantilla del carrito
# del catalogo del inbox 11, renderizado por ``ApprovedTemplate.render`` con
# los valores de ejemplo que Meta guarda en ese mismo catalogo. Que el
# dispatcher de ATT1 mande con el agent bot, como en Johanna, no esta medido
# en la instancia.
CONVERSATION_158 = (
    Path(__file__).parent / "fixtures" / "chatwoot_message_created_inbox_9_conv_158_20260923.json"
)


def _att1_cart_template_message(button_message: dict[str, Any]) -> dict[str, Any]:
    """La plantilla del carrito como la guarda Chatwoot, antes del boton."""
    captured = json.loads(INBOX_11_TEMPLATES.read_text(encoding="utf-8"))
    template = parse_approved_template(
        {"message_templates": captured["templates"]},
        template_name="att1_carrito_abandonado_01",
        expected_language="es_MX",
        expected_category="MARKETING",
        parameter_count=2,
    )
    [example] = [
        component["example"]["body_text"][0]
        for template_entry in captured["templates"]
        if template_entry["name"] == "att1_carrito_abandonado_01"
        for component in template_entry["components"]
        if component["type"] == "BODY"
    ]
    rendered = template.render({"1": example[0], "2": example[1]})
    [template_message, pressed] = json.loads(CONVERSATION_158.read_text(encoding="utf-8"))[
        "messages"
    ]
    assert template_message["message_type"] == 1 and pressed["message_type"] == 0
    assert template_message["sender"]["type"] == "agent_bot"
    message = copy.deepcopy(template_message)
    message["content"] = rendered
    message["conversation_id"] = button_message["conversation_id"]
    message["id"] = button_message["id"] - 1
    # La misma distancia que en la 158 entre la plantilla y el boton.
    message["created_at"] = button_message["created_at"] - (
        pressed["created_at"] - template_message["created_at"]
    )
    return message


def test_the_agent_sees_the_template_before_the_button(tmp_path: Path) -> None:
    [button] = [b for b in _pilot_buttons() if b == "Envíame el enlace"]
    webhook, history = _text_webhook(button)
    template = _att1_cart_template_message(history[0])
    supabase = _TemplateConversation(identities=(FORM_ID,))

    chatwoot, shadow, app = _process(
        tmp_path,
        supabase,
        settings=replace(_manifest_settings(tmp_path), payment_link_enabled=True),
        webhook=webhook,
        history=[template, *history],
        proposal=PAYMENT_LINK_PROPOSAL,
    )

    # El agente ve la plantilla, como mensaje suyo, y despues el boton.
    [(_, context)] = shadow.calls
    assert [
        (message["actor"], message["text"])  # type: ignore[index]
        for message in context["messages"][-2:]  # type: ignore[index]
    ] == [("assistant", template["content"]), ("prospect", button)]
    assert "Alimenta tu Tiroides" in template["content"]
    assert supabase.admission_rpcs == ["portable"]
    [reply] = chatwoot.reply_calls
    assert "src=hermes" in str(reply["content"])
    assert _pending(app) == []


def test_need_help_reaches_the_agent_and_its_handoff_goes_through(
    tmp_path: Path,
) -> None:
    # Decide el agente, como en Johanna (decision 3 de H7). Si deriva, la
    # derivacion pasa sobre el caso que devolvio la admision portable.
    [button] = [b for b in _pilot_buttons() if b == "Necesito ayuda"]
    supabase = _TemplateConversation(identities=(FORM_ID,))

    chatwoot, shadow, app = _press(tmp_path, supabase, button, HANDOFF_PROPOSAL)

    [(_, context)] = shadow.calls
    assert context["messages"][-1]["text"] == button  # type: ignore[index]
    assert set(supabase.admission_rpcs) == {"portable"}
    assert supabase.admission_calls[0] == _att1_admission()
    [handoff] = supabase.handoff_calls
    assert handoff["commercial_case_id"] == "case-1"
    assert chatwoot.calls == [(200, "automation_paused")]
    assert _pending(app) == []


def test_an_answer_to_a_button_is_reauthorized_by_the_portable_too(
    tmp_path: Path,
) -> None:
    # El agente contesta con texto (por ejemplo, pregunta que paso). Antes de
    # responder, la respuesta se reautoriza dos veces contra la admision, y las
    # dos van por la portable. En la base real una conversacion ya adoptada
    # tambien pasaria por la v2; el doble la rechaza para que se vea por que
    # RPC va cada llamada.
    [button] = [b for b in _pilot_buttons() if b == "Necesito ayuda"]
    supabase = _TemplateConversation(identities=(FORM_ID,))

    chatwoot, shadow, app = _press(
        tmp_path, supabase, button, {"decision": "reply", "reply": "Claro, cuéntame qué pasó."}
    )

    assert len(shadow.calls) == 1
    # La admision y las dos reautorizaciones antes de responder.
    assert supabase.admission_rpcs == ["portable"] * 3
    assert supabase.admission_calls == [_att1_admission()] * 3
    [reply] = chatwoot.reply_calls
    assert reply["content"] == "Claro, cuéntame qué pasó."
    assert reply["expected_jid"] == WA_JID
    assert _pending(app) == []


def test_no_more_messages_is_admitted_and_recorded_as_an_opt_out(
    tmp_path: Path,
) -> None:
    # La baja la detecta el camino normal, despues de admitir. El respaldo de
    # f57631f (la baja antes de la admision) no hace falta.
    [button] = [b for b in _pilot_buttons() if b == "No más mensajes"]
    supabase = _TemplateConversation(identities=(FORM_ID,))

    chatwoot, shadow, app = _press(tmp_path, supabase, button, PAYMENT_LINK_PROPOSAL)

    assert supabase.admission_rpcs == ["portable"]
    assert supabase.admission_calls == [_att1_admission()]
    [opt_out] = supabase.opt_outs
    assert opt_out["external_user_id"] == FORM_ID
    assert opt_out["chatwoot_conversation_id"] == 200
    assert opt_out["rule_key"] == "stop_receiving_messages"
    assert shadow.calls == [] and chatwoot.reply_calls == []
    assert _pending(app) == []


def test_without_a_manifest_the_portable_admission_is_never_asked_for(
    tmp_path: Path,
) -> None:
    supabase = _JohannaSupabase()
    webhook, history = _text_webhook("Envíame el enlace")

    chatwoot, shadow, _ = _process(
        tmp_path,
        supabase,
        settings=_johanna_settings(tmp_path),
        webhook=webhook,
        history=history,
    )

    # La admision y las dos reautorizaciones, por la v2 y con los argumentos
    # de siempre: el wa_id textual y el scope de Johanna.
    assert supabase.admission_rpcs == ["v2"] * 3
    assert supabase.admission_calls == [
        _admission_kwargs(WA_ID, scope_key="libre-de-ansiedad-inbound", scope_version=2)
    ] * 3
    assert len(shadow.calls) == 1
    assert len(chatwoot.reply_calls) == 1


# La tercera llamada: la readmision despues de reanudar. Corre sobre la captura
# de la conversacion 177 (inbox 9, un movil mexicano 521 que respondio con la
# conversacion pausada), con el mismo armado que
# tests/test_app.py::test_the_resume_trigger_resumes_a_paused_lead_whose_number_is_not_the_test_number.


class _PausedConversation(_Supabase):
    """La admision da ``blocked`` hasta que se reanuda; despues, ``already_exists``."""

    def __init__(self, *, portable_available: bool) -> None:
        super().__init__()
        self.portable_available = portable_available
        self.resumes: list[dict[str, object]] = []

    def _paused_admission(self, kwargs: dict[str, object]) -> Any:
        self.admission_calls.append(kwargs)
        outcome = "already_exists" if self.resumes else "blocked"
        return InboundCommercialCaseAdmissionResult(
            outcome=outcome,
            commercial_case_id="case-177",
            contact_id="contact-177",
            channel_identity_id="identity-177",
            conversation_id="conversation-177",
            automation_status="disabled" if outcome == "blocked" else "draft_only",
        )

    async def admit_inbound_commercial_case(self, **kwargs: object) -> Any:
        self.admission_rpcs.append("v2")
        if self.portable_available:
            # Con manifiesto, la conversacion adoptada: la v2 sola la rechaza.
            self.admission_calls.append(kwargs)
            raise SupabasePermanentError(
                "inbound_commercial_case_admission_failed: HTTP 400",
                reason="inbound_canonical_conversation_conflict",
            )
        return self._paused_admission(kwargs)

    async def admit_portable_inbound_commercial_case(self, **kwargs: object) -> Any:
        self.admission_rpcs.append("portable")
        if not self.portable_available:
            raise AssertionError("a runtime without a manifest never asks for it")
        return self._paused_admission(kwargs)

    async def resume_paused_conversation(self, **kwargs: object) -> Any:
        self.resumes.append(kwargs)
        return ConversationResumeResult(
            outcome="resumed",
            conversation_id="conversation-177",
            commercial_case_id="case-177",
            resume_event_id="event-177",
        )


@pytest.mark.parametrize("runtime", ["att1", "johanna"])
def test_the_readmission_after_resuming_uses_the_same_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_177)
    fixture = _fixture_177()
    transport = _Chatwoot177(fixture)
    wa_id = JID_DEL_LEAD_177.removesuffix("@s.whatsapp.net")
    if runtime == "att1":
        settings = replace(
            _manifest_settings(tmp_path),
            conversation_resume_enabled=True,
            chatwoot_resume_macro_id=3,
        )
        scope_key, scope_version = _manifest_settings_scope()
    else:
        # Johanna, con los flags de produccion del test de la 177.
        settings = replace(
            _johanna_settings(tmp_path),
            allowed_jid=NUMERO_DE_PRUEBA,
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
            conversation_resume_enabled=True,
            chatwoot_resume_macro_id=3,
        )
        scope_key, scope_version = "libre-de-ansiedad-inbound", 2
    supabase = _PausedConversation(portable_available=runtime == "att1")
    app = create_app(
        settings,
        chatwoot_client=_cliente_177(transport),  # type: ignore[arg-type]
        shadow_processor=StubShadowProcessor(),  # sin propuesta: termina en la admision
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    raw_body = json.dumps(fixture["webhook"], separators=(",", ":")).encode("utf-8")

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=SECRET, delivery="inbound-177", timestamp=AHORA_177),
    )
    asyncio.run(app.state.chatwoot_worker.run_once())

    assert response.status_code == 202
    assert [call["command_key"] for call in supabase.resumes] == ["resume:177:2376"]
    assert transport.macro_executed is True
    # Bloqueada, reanudada y admitida de nuevo: las dos por la misma RPC y con
    # los mismos argumentos.
    expected = "portable" if runtime == "att1" else "v2"
    assert supabase.admission_rpcs == [expected, expected]
    assert supabase.admission_calls == [
        {
            "scope_key": scope_key,
            "scope_version": scope_version,
            "external_conversation_id": 177,
            "external_user_id": wa_id,
        }
    ] * 2
    assert _pending(app) == []
