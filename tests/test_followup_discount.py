"""El seguimiento con cupon, contra conversaciones capturadas de produccion.

Fixtures:
* ``chatwoot_followup_candidates_inbox_9_20260928.json``: la 194 (R1, recibio el
  link y no compro) y la 193 (R2, pregunto y se callo), capturadas de la API de
  Chatwoot el 2026-09-28 y anonimizadas; mas el catalogo real de plantillas del
  inbox 9.
* ``meta_template_url_button_documented_20260928.json``: la plantilla con boton
  de URL dinamica. NO es captura (no existia ninguna con ese boton); se
  reemplaza cuando Meta apruebe la de Johanna.
* ``chatwoot_followup_resolved_conversations_inbox_9_20261001.json``: la 201
  (R1) que el equipo marco resuelta 16 h despues del link sin escribirle,
  capturada el 2026-10-01 y anonimizada, con las paginas de abiertas y
  resueltas del inbox 9.
* ``chatwoot_message_templates_inbox_9_20261001.json``: el catalogo del inbox 9
  por la API (2026-10-01), con la plantilla de Johanna ya aprobada por Meta:
  idioma ``en`` y el cupon tambien en el texto (``{{3}}``).
* ``chatwoot_inbox_11_message_templates_20261010.json``: el catalogo del inbox
  11 (ATT1) por la API (2026-10-10), con ``att1_seguimiento_descuento_01``
  aprobada: solo ``{{1}}`` y ``{{2}}``, el cupon va solo en el boton.
* ``chatwoot_followup_candidate_inbox_11_20261010.json``: la 21 de ATT1
  (respondio la plantilla del carrito, recibio el link del agente y se callo),
  capturada el 2026-10-10 y anonimizada, con las paginas del inbox 11.
* ``ghl/expected/ghl_form_webhook_landing_d_derived_20260929.lead_precheckout.json``:
  el formulario de la landing -d de ATT1, con el sck real del anuncio, que es
  el que lleva el link de la reserva portable.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from bridge.chatwoot import ChatwootProtocolError
from bridge.chatwoot_inbox import RetryableChatwootWorkError
from bridge.followup_discount import (
    REGIME_LINK_SENT,
    REGIME_PAYMENT_FAILED,
    REGIME_WENT_QUIET,
    ConversationFollowupSweeper,
    evaluate_followup_candidate,
    followup_button_suffix,
    parse_followup_template,
)
from bridge.phones import canonical_whatsapp_phone
from bridge.reactivation import evaluate_reactivation_candidate
from bridge.supabase import FOLLOWUP_CLAIM_OUTCOMES


FIXTURES = Path(__file__).parent / "fixtures"
CAPTURED = json.loads(
    (FIXTURES / "chatwoot_followup_candidates_inbox_9_20260928.json").read_text(
        encoding="utf-8"
    )
)
DOCUMENTED = json.loads(
    (FIXTURES / "meta_template_url_button_documented_20260928.json").read_text(
        encoding="utf-8"
    )
)
RESOLVED = json.loads(
    (
        FIXTURES / "chatwoot_followup_resolved_conversations_inbox_9_20261001.json"
    ).read_text(encoding="utf-8")
)
JOHANNA_CATALOG = json.loads(
    (FIXTURES / "chatwoot_message_templates_inbox_9_20261001.json").read_text(
        encoding="utf-8"
    )
)
ATT1_CATALOG = json.loads(
    (FIXTURES / "chatwoot_inbox_11_message_templates_20261010.json").read_text(
        encoding="utf-8"
    )
)
ATT1_CANDIDATE = json.loads(
    (FIXTURES / "chatwoot_followup_candidate_inbox_11_20261010.json").read_text(
        encoding="utf-8"
    )
)
# La atribucion del formulario de la landing -d de ATT1 (el golden del
# adaptador de GHL, derivado de la captura del 2026-09-29): el sck del anuncio
# es el real, textual; el fbclid es sintetico. Es lo que la reserva portable
# copia al link del caso del formulario.
ATT1_FORM_ATTRIBUTION = json.loads(
    (
        FIXTURES
        / "ghl"
        / "expected"
        / "ghl_form_webhook_landing_d_derived_20260929.lead_precheckout.json"
    ).read_text(encoding="utf-8")
)["raw_payload"]["data"]["attribution"]
TEMPLATE = "johanna_seguimiento_descuento_01"
COUPON = "JOHANNA10"
ATT1_TEMPLATE = "att1_seguimiento_descuento_01"
ATT1_COUPON = "TIROIDES10"
ATT1_PRODUCT = "Alimenta tu Tiroides"
HOUR = 3_600


def _conversation(conversation_id: str) -> tuple[dict, list[dict]]:
    if conversation_id == "201":
        entry = copy.deepcopy(RESOLVED["conversation"])
    else:
        entry = copy.deepcopy(CAPTURED["conversations"][conversation_id])
    return entry["conversation"], entry["messages"]


def _last_inbound_at(messages: list[dict]) -> int:
    return max(
        message["created_at"]
        for message in messages
        if message["message_type"] == 0
    )


def _evaluate(details: dict, messages: list[dict], *, hours_after: float):
    now = _last_inbound_at(messages) + int(hours_after * HOUR)
    return evaluate_followup_candidate(
        details, messages, expected_inbox_id=9, now_epoch=now
    )


# --- el criterio sobre los casos reales -------------------------------------


def test_r1_received_the_link_and_went_quiet_is_a_candidate() -> None:
    details, messages = _conversation("194")
    decision = _evaluate(details, messages, hours_after=24.5)
    candidate = decision.candidate
    assert candidate is not None, decision.skip_reason
    assert candidate.regime == REGIME_LINK_SENT
    assert candidate.last_inbound_message_id == 2444
    assert candidate.last_outbound_message_id == 2445
    assert candidate.command_key == "followup:194:2445"
    assert candidate.external_user_id == "593900000194"
    assert candidate.email == "lead194@example.com"


def test_r2_asked_and_went_quiet_is_a_candidate() -> None:
    details, messages = _conversation("193")
    decision = _evaluate(details, messages, hours_after=30)
    candidate = decision.candidate
    assert candidate is not None, decision.skip_reason
    assert candidate.regime == REGIME_WENT_QUIET
    assert candidate.last_inbound_message_id == 2437
    # El agente contesto en dos partes: el ancla es la ultima.
    assert candidate.last_outbound_message_id == 2439


@pytest.mark.parametrize(
    ("hours_after", "reason"),
    [(23.9, "inbound_too_recent"), (72.1, "inbound_too_old")],
)
def test_the_window_is_24_to_72_hours_from_the_lead(hours_after: float, reason: str) -> None:
    details, messages = _conversation("194")
    assert _evaluate(details, messages, hours_after=hours_after).skip_reason == reason


def test_a_lead_who_never_replied_never_gets_a_followup() -> None:
    # La regla de Dan: solo a quien ya nos contesto. La 193 sin el mensaje del
    # lead es el primer toque solo, que es lo que tienen 103 de 154.
    details, messages = _conversation("193")
    only_ours = [message for message in messages if message["message_type"] != 0]
    now = only_ours[-1]["created_at"] + 30 * HOUR
    decision = evaluate_followup_candidate(
        details, only_ours, expected_inbox_id=9, now_epoch=now
    )
    assert decision.skip_reason == "never_replied"


def test_when_the_lead_wrote_last_it_is_the_agents_turn() -> None:
    details, messages = _conversation("194")
    without_reply = [message for message in messages if message["id"] != 2445]
    assert (
        _evaluate(details, without_reply, hours_after=30).skip_reason
        == "last_message_inbound"
    )


def test_a_paused_conversation_belongs_to_the_team() -> None:
    details, messages = _conversation("194")
    details["labels"] = ["automation_paused"]
    assert _evaluate(details, messages, hours_after=30).skip_reason == "conversation_paused"


def test_an_opted_out_contact_is_skipped() -> None:
    details, messages = _conversation("194")
    details["labels"] = ["automation_opted_out"]
    assert _evaluate(details, messages, hours_after=30).skip_reason == "contact_opted_out"


def test_a_last_message_from_a_person_of_the_team_is_not_ours_to_follow() -> None:
    details, messages = _conversation("193")
    messages[-1]["sender"] = {"type": "user", "id": 5}
    assert (
        _evaluate(details, messages, hours_after=30).skip_reason
        == "last_message_not_from_agent"
    )


def test_a_conversation_the_team_resolved_is_still_a_candidate() -> None:
    # La 201: el agente le mando el link, la persona se callo y el equipo la
    # marco resuelta para ordenar la bandeja, sin escribirle. Sigue siendo R1.
    details, messages = _conversation("201")
    assert details["status"] == "resolved" and details["labels"] == []
    assert any(message["message_type"] == 2 for message in messages)
    decision = _evaluate(details, messages, hours_after=30)
    assert decision.eligible
    assert decision.candidate.regime == REGIME_LINK_SENT
    assert decision.candidate.last_inbound_message_id == 2506
    assert decision.candidate.last_outbound_message_id == 2507


@pytest.mark.parametrize("status", ["pending", "snoozed"])
def test_pending_and_snoozed_conversations_are_still_skipped(status: str) -> None:
    details, messages = _conversation("201")
    details["status"] = status
    assert (
        _evaluate(details, messages, hours_after=30).skip_reason
        == "conversation_status_excluded"
    )


@pytest.mark.parametrize(
    ("labels", "reason"),
    [
        (["automation_paused"], "conversation_paused"),
        (["automation_opted_out"], "contact_opted_out"),
    ],
)
def test_resolving_does_not_skip_any_barrier(labels: list[str], reason: str) -> None:
    details, messages = _conversation("201")
    details["labels"] = labels
    assert _evaluate(details, messages, hours_after=30).skip_reason == reason


def test_the_payment_failure_template_marks_its_regime() -> None:
    # La API no devuelve template_params: el regimen sale del texto.
    details, messages = _conversation("193")
    messages[0]["content"] = (
        "Hola, NOMBRE. Soy el asistente virtual del equipo de la Psic. Johanna. "
        "Tu compra de Liberate de la Ansiedad no pudo completarse."
    )
    candidate = _evaluate(details, messages, hours_after=30).candidate
    assert candidate is not None
    assert candidate.regime == REGIME_PAYMENT_FAILED


# --- no se pisa con la reactivacion ------------------------------------------


@pytest.mark.parametrize("conversation_id", ["194", "193"])
def test_a_followup_candidate_is_never_a_reactivation_candidate(conversation_id: str) -> None:
    details, messages = _conversation(conversation_id)
    details["can_reply"] = False
    now = _last_inbound_at(messages) + 30 * HOUR
    followup = evaluate_followup_candidate(details, messages, expected_inbox_id=9, now_epoch=now)
    reactivation = evaluate_reactivation_candidate(
        details, messages, expected_inbox_id=9, now_epoch=now
    )
    assert followup.eligible
    assert reactivation.skip_reason == "last_message_not_inbound"


def test_a_reactivation_candidate_is_never_a_followup_candidate() -> None:
    details, messages = _conversation("194")
    details["can_reply"] = False
    lead_last = [message for message in messages if message["id"] != 2445]
    now = _last_inbound_at(lead_last) + 30 * HOUR
    reactivation = evaluate_reactivation_candidate(
        details, lead_last, expected_inbox_id=9, now_epoch=now
    )
    followup = evaluate_followup_candidate(details, lead_last, expected_inbox_id=9, now_epoch=now)
    assert reactivation.eligible, reactivation.skip_reason
    assert followup.skip_reason == "last_message_inbound"


# --- la plantilla y el boton ---------------------------------------------------


def test_the_documented_template_parses_with_one_dynamic_checkout_button() -> None:
    template = parse_followup_template(DOCUMENTED, template_name=TEMPLATE, expected_language="es_EC")
    params = template.params(
        first_name="Maria",
        product_name="Libérate de la Ansiedad",
        coupon_code=COUPON,
        button_suffix="F106691755G?off=bxjge6zq&checkoutMode=10&offDiscount=JOHANNA10",
    )
    assert params["processed_params"] == {
        "body": {"1": "Maria", "2": "Libérate de la Ansiedad", "3": COUPON},
        "buttons": [
            {
                "type": "url",
                "parameter": "F106691755G?off=bxjge6zq&checkoutMode=10&offDiscount=JOHANNA10",
            }
        ],
    }
    rendered = template.render(first_name="Maria", product_name="Libérate de la Ansiedad", coupon_code=COUPON)
    assert "{{" not in rendered and COUPON in rendered


def test_the_real_catalog_has_no_followup_template_yet() -> None:
    real = {"message_templates": CAPTURED["inbox_9_message_templates"]}
    with pytest.raises(ChatwootProtocolError, match="followup_template_not_found"):
        parse_followup_template(real, template_name=TEMPLATE)


def test_the_real_att1_template_parses_with_the_coupon_only_in_the_button() -> None:
    # Decision de Dan del 2026-10-01: en ATT1 el codigo no va en el texto, va
    # solo en el boton (offDiscount). La plantilla aprobada tiene {{1}} y {{2}}.
    template = parse_followup_template(
        ATT1_CATALOG, template_name=ATT1_TEMPLATE, expected_language="es_MX"
    )
    assert template.carries_coupon is False
    assert (template.language, template.category) == ("es_MX", "MARKETING")

    # El link real que el agente le mando a la 21 (sck del anuncio y fbclid
    # redactados en la captura): el seguimiento emite uno igual con otro ULID.
    link = next(
        word
        for word in ATT1_CANDIDATE["conversation"]["messages"][-1]["content"].split()
        if word.startswith("https://pay.hotmart.com/")
    )
    suffix = followup_button_suffix(link, ATT1_COUPON)
    params = template.params(
        first_name="María",
        product_name=ATT1_PRODUCT,
        coupon_code=ATT1_COUPON,
        button_suffix=suffix,
    )
    assert (params["name"], params["language"]) == (ATT1_TEMPLATE, "es_MX")
    # Dos parametros para dos marcadores: con el "3" de mas Meta rechaza el
    # envio (#132000) despues de que Chatwoot lo acepto.
    body = params["processed_params"]["body"]
    assert body == {"1": "María", "2": ATT1_PRODUCT}
    assert list(body) == ["1", "2"]
    assert params["processed_params"]["buttons"] == [{"type": "url", "parameter": suffix}]
    assert suffix.startswith("D98014973Y?off=2uafw5bg&checkoutMode=10&src=hermes&sck=")
    assert "SCK_ANUNCIO_REDACTADO~hermes~v1~" in suffix
    assert suffix.endswith("&offDiscount=TIROIDES10")

    rendered = template.render(
        first_name="María", product_name=ATT1_PRODUCT, coupon_code=ATT1_COUPON
    )
    assert rendered.startswith("Hola, María. ")
    assert ATT1_PRODUCT in rendered
    assert "{{" not in rendered
    assert ATT1_COUPON not in rendered


def test_the_button_takes_the_link_the_portable_reservation_emits_for_att1() -> None:
    # En la captura de la 21 el sck del anuncio esta redactado. El link que
    # emite la reserva portable para ese caso (lo prueba
    # validate_portable_conversation_followup.mjs) lleva el sck y el fbclid
    # del formulario de la landing -d: el sck real del anuncio de ATT1, con ~
    # y guiones, y el marcador del recuperador con otro ULID. El boton lo
    # toma entero, sin escapar nada, y le suma el cupon.
    sck = ATT1_FORM_ATTRIBUTION["sck"]
    assert sck.startswith("meta-ads~") and "-" in sck.removeprefix("meta-ads~")
    link = (
        ATT1_AGENT_LINK.split("&sck=")[0]
        + f"&sck={sck}~hermes~v1~01K5ABCDEFX2VYB4M6X9CDPTF1"
        + f"&fbclid={ATT1_FORM_ATTRIBUTION['fbclid']}"
    )

    suffix = followup_button_suffix(link, ATT1_COUPON)

    assert suffix == (
        link.removeprefix("https://pay.hotmart.com/") + "&offDiscount=TIROIDES10"
    )
    assert f"&sck={sck}~hermes~v1~" in suffix


def test_the_real_johanna_template_still_carries_the_coupon_in_the_text() -> None:
    # La de Johanna aprobada por Meta y capturada por la API: el idioma con que
    # quedo registrada es 'en', y el cupon va en el texto y en el boton.
    template = parse_followup_template(
        JOHANNA_CATALOG, template_name=TEMPLATE, expected_language="en"
    )
    assert template.carries_coupon is True
    assert template.language == "en"

    params = template.params(
        first_name="María",
        product_name="Libérate de la Ansiedad",
        coupon_code=COUPON,
        button_suffix="F106691755G?off=bxjge6zq&checkoutMode=10&offDiscount=JOHANNA10",
    )
    body = params["processed_params"]["body"]
    assert body == {"1": "María", "2": "Libérate de la Ansiedad", "3": COUPON}
    # El mismo orden de siempre: el cuerpo que manda Johanna no cambia.
    assert list(body) == ["1", "2", "3"]
    rendered = template.render(
        first_name="María", product_name="Libérate de la Ansiedad", coupon_code=COUPON
    )
    assert "{{" not in rendered and COUPON in rendered


@pytest.mark.parametrize(
    ("catalog", "name"),
    [(DOCUMENTED, TEMPLATE), (ATT1_CATALOG, ATT1_TEMPLATE)],
    ids=["johanna", "att1"],
)
@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda t: t.update(status="PENDING"), "followup_template_not_approved"),
        (
            lambda t: t["components"][1]["buttons"][0].update(type="QUICK_REPLY"),
            "followup_template_button_not_dynamic_checkout",
        ),
        (
            lambda t: t["components"][1]["buttons"][0].update(url="https://example.com/{{1}}"),
            "followup_template_button_not_dynamic_checkout",
        ),
        (lambda t: t["components"].pop(1), "followup_template_button_missing"),
        (
            lambda t: t["components"][0].update(text="Hola, {{1}}. Codigo {{3}}."),
            "followup_template_unexpected_placeholders",
        ),
        # Solo el nombre: falta el producto.
        (
            lambda t: t["components"][0].update(text="Hola, {{1}}."),
            "followup_template_unexpected_placeholders",
        ),
        # Un marcador que el bridge no sabe llenar.
        (
            lambda t: t["components"][0].update(
                text="Hola, {{1}}. Sobre {{2}}. Codigo {{4}}."
            ),
            "followup_template_unexpected_placeholders",
        ),
        # Los tres de Johanna y uno mas (alguien suma una fecha en Meta): el
        # bridge mandaria tres parametros para cuatro marcadores y Meta lo
        # rechazaria con #132000 despues de que Chatwoot lo acepto. Un parser
        # que mirara solo los tres primeros la dejaria pasar.
        (
            lambda t: t["components"][0].update(
                text="Hola, {{1}}. Sobre {{2}}. Codigo {{3}}. Vence {{4}}."
            ),
            "followup_template_unexpected_placeholders",
        ),
    ],
)
def test_a_template_that_is_not_exactly_ours_fails_closed(
    catalog: dict, name: str, mutate, error: str
) -> None:
    payload = copy.deepcopy(catalog)
    [template] = [t for t in payload["message_templates"] if t["name"] == name]
    mutate(template)
    with pytest.raises(ChatwootProtocolError, match=error):
        parse_followup_template(payload, template_name=name)


def test_the_button_carries_the_issued_link_plus_the_coupon() -> None:
    # El link real que el agente mando en la 194 (sck del anuncio y fbclid
    # redactados en la captura): el seguimiento emite uno igual con otro ULID.
    link = next(
        word
        for word in CAPTURED["conversations"]["194"]["messages"][-1]["content"].split()
        if word.startswith("https://pay.hotmart.com/")
    )
    suffix = followup_button_suffix(link, COUPON)
    assert suffix.startswith("F106691755G?off=mgbgpp19&checkoutMode=10&src=hermes&sck=")
    assert "%7Chermes%7Cv1%7C" in suffix
    assert suffix.endswith("&offDiscount=JOHANNA10")


@pytest.mark.parametrize(
    ("url", "coupon"),
    [
        ("https://example.com/F106691755G?off=x", COUPON),
        ("https://pay.hotmart.com/F106691755G?off=x", "JOHANNA 10"),
        ("https://pay.hotmart.com/F106691755G?off=x y", COUPON),
    ],
)
def test_a_bad_link_or_coupon_never_reaches_the_button(url: str, coupon: str) -> None:
    with pytest.raises(ValueError):
        followup_button_suffix(url, coupon)


# --- el worker de punta a punta, con dobles -----------------------------------


ISSUED_URL = (
    "https://pay.hotmart.com/F106691755G?off=mgbgpp19&checkoutMode=10&src=hermes"
    "&sck=hermes%7Cv1%7C01K5ABCDEFX2VYB4M6X9CDPTF1"
)


class _FakeChatwoot:
    def __init__(
        self,
        conversations: list[str],
        *,
        fail_send: bool = False,
        inbox_payload: dict | None = None,
    ) -> None:
        self.entries = []
        for conversation_id in conversations:
            details, messages = _conversation(conversation_id)
            self.entries.append({"conversation": details, "messages": messages})
        self.fail_send = fail_send
        self.inbox_payload = DOCUMENTED if inbox_payload is None else inbox_payload
        self.sent: list[dict] = []
        self.overrides: dict[int, list[dict]] = {}
        self.listings: list[dict] = []
        self.inbox_reads: list[int] = []
        self.rereads: list[int] = []

    async def get_inbox(self, *, inbox_id: int) -> dict:
        self.inbox_reads.append(inbox_id)
        return copy.deepcopy(self.inbox_payload)

    async def list_recent_conversations_with_messages(self, **kwargs: object) -> list[dict]:
        self.listings.append(kwargs)
        return copy.deepcopy(self.entries)

    async def get_conversation_messages(self, *, conversation_id: int, limit: int) -> list[dict]:
        self.rereads.append(conversation_id)
        if conversation_id in self.overrides:
            return self.overrides[conversation_id]
        for entry in self.entries:
            if entry["conversation"]["id"] == conversation_id:
                return copy.deepcopy(entry["messages"])
        raise AssertionError(conversation_id)

    async def send_followup_template(self, **kwargs: object) -> dict:
        if self.fail_send:
            raise ChatwootProtocolError("invalid_sent_message")
        self.sent.append(kwargs)
        return {"status": "sent", "message_id": 9000 + len(self.sent)}


class _FakeSupabase:
    def __init__(
        self, claim_outcome: str = "claimed", *, issued_url: str = ISSUED_URL
    ) -> None:
        self.claim_outcome = claim_outcome
        self.issued_url = issued_url
        self.claims: list[dict] = []
        self.authorizations: list[dict] = []
        self.finalized: list[dict] = []
        self.settled: list[dict] = []

    async def get_lead_first_name_inference(self, name_key: str) -> None:
        return None

    async def claim_conversation_followup(self, **kwargs: object) -> SimpleNamespace:
        self.claims.append(kwargs)
        claimed = self.claim_outcome == "claimed"
        return SimpleNamespace(
            outcome=self.claim_outcome,
            followup_event_id="evt" if claimed else None,
            checkout_issuance_id=f"iss-{kwargs['external_conversation_id']}" if claimed else None,
            checkout_url_final=self.issued_url if claimed else None,
            sck_value="hermes|v1|01K5ABCDEFX2VYB4M6X9CDPTF1" if claimed else None,
        )

    async def authorize_chatwoot_checkout_issuance_v2(self, **kwargs: object) -> SimpleNamespace:
        self.authorizations.append(kwargs)
        return SimpleNamespace(outcome="request_started")

    async def finalize_chatwoot_checkout_issuance_v2(self, **kwargs: object) -> None:
        self.finalized.append(kwargs)

    async def settle_conversation_followup(self, **kwargs: object) -> None:
        self.settled.append(kwargs)


def _sweeper(
    chatwoot: _FakeChatwoot,
    supabase: _FakeSupabase,
    *,
    hours_after: float = 30,
    **options: object,
):
    newest_inbound = max(
        _last_inbound_at(entry["messages"]) for entry in chatwoot.entries
    )
    return ConversationFollowupSweeper(
        chatwoot=chatwoot,
        supabase=supabase,
        account_id=1,
        inbox_id=9,
        template_name=TEMPLATE,
        coupon_code=COUPON,
        product_name="Libérate de la Ansiedad",
        expected_template_language="es_EC",
        clock=lambda: newest_inbound + hours_after * HOUR,
        ulid_factory=lambda: "01K5ABCDEFX2VYB4M6X9CDPTF1",
        **options,
    )


def test_the_sweeper_sends_one_followup_per_real_case() -> None:
    chatwoot = _FakeChatwoot(["194", "193"])
    supabase = _FakeSupabase()
    sweeper = _sweeper(chatwoot, supabase, hours_after=25)
    assert asyncio.run(sweeper.run_once()) == 2

    by_conversation = {claim["external_conversation_id"]: claim for claim in supabase.claims}
    assert by_conversation[194]["regime"] == REGIME_LINK_SENT
    assert by_conversation[193]["regime"] == REGIME_WENT_QUIET
    assert by_conversation[194]["last_outbound_message_id"] == 2445
    assert by_conversation[194]["contact_email"] == "lead194@example.com"
    assert by_conversation[194]["coupon_code"] == COUPON

    # El link se autoriza con el mismo ancla con el que se emitio.
    assert {a["trigger_external_message_id"] for a in supabase.authorizations} == {"2445", "2439"}

    greetings = {
        sent["conversation_id"]: sent["template_params"]["processed_params"]["body"]["1"]
        for sent in chatwoot.sent
    }
    # El saludo sale de la cadena del primer nombre (#201/#202), no del nombre
    # completo que guardo Chatwoot.
    assert greetings == {194: "Maria", 193: "Juan"}
    for sent in chatwoot.sent:
        button = sent["template_params"]["processed_params"]["buttons"][0]
        assert button["type"] == "url"
        assert button["parameter"] == ISSUED_URL.removeprefix("https://pay.hotmart.com/") + "&offDiscount=JOHANNA10"
        assert sent["command_key"].startswith("followup:")

    assert [f["status"] for f in supabase.finalized] == ["accepted_by_chatwoot"] * 2
    assert [s["status"] for s in supabase.settled] == ["sent"] * 2
    assert sweeper.last_scan_summary.startswith("scanned=2 sent=2")


def test_the_sweeper_reads_resolved_conversations_inside_the_window() -> None:
    # Antes el barrido pedia solo las abiertas y la 201 no le llegaba nunca.
    chatwoot = _FakeChatwoot(["194", "201"])
    supabase = _FakeSupabase()
    sweeper = _sweeper(chatwoot, supabase, hours_after=25)
    now = int(sweeper._clock())
    assert asyncio.run(sweeper.run_once()) == 2

    assert chatwoot.listings == [
        {
            "expected_inbox_id": 9,
            "statuses": ("open", "resolved"),
            "active_since_epoch": now - 72 * HOUR - HOUR,
            "max_pages": 5,
        }
    ]
    by_conversation = {claim["external_conversation_id"]: claim for claim in supabase.claims}
    assert by_conversation[201]["regime"] == REGIME_LINK_SENT
    assert by_conversation[201]["last_outbound_message_id"] == 2507
    assert {sent["conversation_id"] for sent in chatwoot.sent} == {194, 201}


def test_a_durable_barrier_stops_the_send_and_names_itself() -> None:
    chatwoot = _FakeChatwoot(["194"])
    supabase = _FakeSupabase(claim_outcome="purchase_already_approved")
    sweeper = _sweeper(chatwoot, supabase)
    assert asyncio.run(sweeper.run_once()) == 0
    assert chatwoot.sent == [] and supabase.authorizations == []
    assert "purchase_already_approved=1" in sweeper.last_scan_summary
    assert sweeper.last_scan_state == "healthy"


def test_if_the_lead_writes_meanwhile_nothing_is_reserved() -> None:
    chatwoot = _FakeChatwoot(["194"])
    _, messages = _conversation("194")
    messages.append(
        {**messages[1], "id": 2500, "created_at": messages[-1]["created_at"] + 10}
    )
    chatwoot.overrides[194] = messages
    supabase = _FakeSupabase()
    sweeper = _sweeper(chatwoot, supabase)
    assert asyncio.run(sweeper.run_once()) == 0
    assert supabase.claims == []
    assert "lead_wrote_meanwhile=1" in sweeper.last_scan_summary


def test_an_uncertain_send_is_never_retried() -> None:
    chatwoot = _FakeChatwoot(["194"], fail_send=True)
    supabase = _FakeSupabase()
    sweeper = _sweeper(chatwoot, supabase)
    assert asyncio.run(sweeper.run_once()) == 0
    # El link queda 'delivery_unknown': la base no deja volver a mandarlo.
    assert [f["status"] for f in supabase.finalized] == ["delivery_unknown"]
    assert [s["status"] for s in supabase.settled] == ["failed"]
    assert sweeper.last_scan_state == "error"


def test_a_missing_or_invalid_coupon_never_builds_a_sweeper() -> None:
    for coupon in ("", "JOHANNA 10", "X" * 65):
        with pytest.raises(ValueError):
            ConversationFollowupSweeper(
                chatwoot=object(),
                supabase=object(),
                account_id=1,
                inbox_id=9,
                template_name=TEMPLATE,
                coupon_code=coupon,
                product_name="Libérate de la Ansiedad",
            )


# --- con manifiesto (ATT1): la identidad, el horario, el nombre y la prueba ----


MEXICO_CITY = ZoneInfo("America/Mexico_City")
# La 21 de ATT1 (F4): la persona contesto la plantilla del carrito el 09/10 a
# las 10:15 de CDMX y el agente le mando el link a las 10:30.
ATT1_LEAD_REPLIED_AT = _last_inbound_at(ATT1_CANDIDATE["conversation"]["messages"])
# El link real que el agente le mando: la reserva del seguimiento emite uno
# igual con otro ULID.
ATT1_AGENT_LINK = next(
    word
    for word in ATT1_CANDIDATE["conversation"]["messages"][-1]["content"].split()
    if word.startswith("https://pay.hotmart.com/")
)


def _att1_chatwoot() -> _FakeChatwoot:
    """La 21 de ATT1 (F4) y el catalogo real del inbox 11 (F1)."""
    chatwoot = _FakeChatwoot([], inbox_payload=ATT1_CATALOG)
    chatwoot.entries.append(copy.deepcopy(ATT1_CANDIDATE["conversation"]))
    return chatwoot


class _StoredIdentity:
    """El resolvedor del entrante cuando el caso nacio del formulario.

    El formulario guarda el movil de Mexico como 52 + 10 y Chatwoot lo trae
    como el wa_id, 521 + 10: el resolvedor devuelve la forma guardada, que es
    ``canonical_whatsapp_phone`` del wa_id (la funcion real).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def __call__(self, wa_id: str, *, conversation_id: object) -> str:
        self.calls.append((wa_id, conversation_id))
        stored = canonical_whatsapp_phone(wa_id)
        assert stored is not None
        return stored


def _mexico_city(day: int, hour: int, minute: int = 0) -> float:
    """Un instante de octubre de 2026 en la hora de CDMX, la zona de ATT1."""
    return datetime(2026, 10, day, hour, minute, tzinfo=MEXICO_CITY).timestamp()


def _att1_sweeper(
    chatwoot: _FakeChatwoot,
    supabase: _FakeSupabase,
    *,
    now: float,
    **overrides: object,
) -> ConversationFollowupSweeper:
    """El barredor con las opciones que le pasa el bridge con el manifiesto."""
    options: dict[str, object] = {
        "send_hours": (9, 21),
        "time_zone": MEXICO_CITY,
        "refuse_unsafe_greeting": True,
        "phone_equivalence": True,
        "external_user_id_resolver": _StoredIdentity(),
        **overrides,
    }
    return ConversationFollowupSweeper(
        chatwoot=chatwoot,
        supabase=supabase,
        account_id=2,
        inbox_id=11,
        template_name=ATT1_TEMPLATE,
        coupon_code=ATT1_COUPON,
        product_name=ATT1_PRODUCT,
        expected_template_language="es_MX",
        clock=lambda: now,
        ulid_factory=lambda: "01K5ABCDEFX2VYB4M6X9CDPTF1",
        **options,
    )


def test_only_the_test_phone_receives_and_the_rest_are_counted() -> None:
    # El modo de prueba antes de abrir: solo el telefono de prueba recibe. La
    # otra candidata no se relee ni se reserva, solo se cuenta.
    chatwoot = _FakeChatwoot(["194", "193"])
    supabase = _FakeSupabase()
    sweeper = _sweeper(chatwoot, supabase, hours_after=25, only_phone="+593900000194")
    assert sweeper.only_phone == "+593900000194"

    assert asyncio.run(sweeper.run_once()) == 1
    assert [claim["external_conversation_id"] for claim in supabase.claims] == [194]
    assert [sent["conversation_id"] for sent in chatwoot.sent] == [194]
    assert chatwoot.rereads == [194]
    # Cuenta candidatas segun Chatwoot, antes de las barreras de la base: es
    # una cota superior, no a cuantas personas les habria salido.
    assert sweeper.last_scan_summary == "scanned=2 sent=1 held_only_phone=1"
    assert sweeper.last_scan_state == "healthy"


@pytest.mark.parametrize("only_phone", ["+525500000021", "+5215500000021"])
def test_the_test_phone_is_compared_in_canonical_form(only_phone: str) -> None:
    # Chatwoot guarda el movil de Mexico con el 1 (521...). Escrito como 52...
    # en el archivo del bridge es el mismo telefono y recibe igual.
    chatwoot = _att1_chatwoot()
    assert (
        chatwoot.entries[0]["conversation"]["meta"]["sender"]["phone_number"]
        == "+5215500000021"
    )
    supabase = _FakeSupabase(issued_url=ATT1_AGENT_LINK)
    sweeper = _att1_sweeper(
        chatwoot, supabase, now=ATT1_LEAD_REPLIED_AT + 30 * HOUR, only_phone=only_phone
    )
    assert asyncio.run(sweeper.run_once()) == 1
    assert [sent["conversation_id"] for sent in chatwoot.sent] == [21]
    assert sweeper.last_scan_summary == "scanned=1 sent=1"


def test_the_held_candidates_are_always_in_the_summary() -> None:
    # El resumen muestra los cuatro motivos mas frecuentes. Las retenidas por
    # el modo de prueba son lo que se mira en /ready antes de abrir: se ven
    # aunque haya cinco motivos mas frecuentes, y other_reasons no las cuenta.
    chatwoot = _FakeChatwoot(["194", "193"])
    base_details, base_messages = _conversation("194")
    mutations = [
        lambda details: details.update(labels=["automation_paused"]),
        lambda details: details.update(labels=["automation_opted_out"]),
        lambda details: details.update(status="pending"),
        lambda details: details.update(muted=True),
        lambda details: details.update(snoozed_until=1_791_000_000),
    ]
    conversation_id = 1_000
    for mutate in mutations:
        for _ in range(2):
            details = copy.deepcopy(base_details)
            details["id"] = conversation_id
            conversation_id += 1
            mutate(details)
            chatwoot.entries.append(
                {"conversation": details, "messages": copy.deepcopy(base_messages)}
            )
    supabase = _FakeSupabase()
    sweeper = _sweeper(chatwoot, supabase, hours_after=25, only_phone="+593900000194")

    assert asyncio.run(sweeper.run_once()) == 1
    assert sweeper.last_scan_summary == (
        "scanned=12 sent=1 conversation_paused=2 contact_opted_out=2 "
        "conversation_status_excluded=2 conversation_muted=2 other_reasons=1 "
        "held_only_phone=1"
    )


def test_outside_the_send_hours_nothing_is_read() -> None:
    # La 21 es candidata desde el 10/10 a las 10:15 de CDMX. A las 03:00 del
    # 11/10 lo sigue siendo, pero esta fuera de 09-21: no se lee el catalogo
    # ni Chatwoot, y el barrido no es una falla.
    chatwoot = _att1_chatwoot()
    supabase = _FakeSupabase(issued_url=ATT1_AGENT_LINK)
    night = _att1_sweeper(chatwoot, supabase, now=_mexico_city(11, 3))
    assert night.send_hours == (9, 21)

    assert asyncio.run(night.run_once()) == 0
    assert chatwoot.inbox_reads == []
    assert chatwoot.listings == []
    assert chatwoot.rereads == []
    assert supabase.claims == []
    assert night.last_scan_state == "healthy"
    assert night.last_scan_summary == "outside_send_hours"
    assert night.last_sent_count == 0
    assert night._has_completed_scan is True

    morning = _att1_sweeper(chatwoot, supabase, now=_mexico_city(11, 10))
    assert asyncio.run(morning.run_once()) == 1
    assert chatwoot.inbox_reads == [11]
    assert [sent["conversation_id"] for sent in chatwoot.sent] == [21]
    assert morning.last_scan_state == "healthy"
    assert morning.last_scan_summary == "scanned=1 sent=1"


@pytest.mark.parametrize(
    ("send_hours", "hour", "minute", "reads"),
    [
        ((9, 21), 8, 59, False),
        ((9, 21), 9, 0, True),
        ((9, 21), 20, 59, True),
        ((9, 21), 21, 0, False),
        # 00-24 es a cualquier hora, escrito a proposito.
        ((0, 24), 0, 0, True),
        ((0, 24), 23, 59, True),
    ],
)
def test_the_send_hours_include_the_start_and_leave_out_the_end(
    send_hours: tuple[int, int], hour: int, minute: int, reads: bool
) -> None:
    chatwoot = _att1_chatwoot()
    sweeper = _att1_sweeper(
        chatwoot,
        _FakeSupabase(issued_url=ATT1_AGENT_LINK),
        now=_mexico_city(11, hour, minute),
        send_hours=send_hours,
    )
    asyncio.run(sweeper.run_once())
    assert (chatwoot.inbox_reads == [11]) is reads
    assert (sweeper.last_scan_summary == "outside_send_hours") is not reads


def test_an_unsafe_greeting_is_refused_before_reserving(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Chatwoot guarda el push name de WhatsApp, que puede ser un telefono
    # (reactivation_first_name). Sin primer nombre el saludo cae al nombre
    # completo y la plantilla saldria con "Hola, +593 ...".
    phone_as_name = "+593 90 000 0193"

    def chatwoot_with_a_phone_as_name() -> _FakeChatwoot:
        chatwoot = _FakeChatwoot(["194", "193"])
        chatwoot.entries[1]["conversation"]["meta"]["sender"]["name"] = phone_as_name
        return chatwoot

    chatwoot = chatwoot_with_a_phone_as_name()
    supabase = _FakeSupabase()
    sweeper = _sweeper(chatwoot, supabase, hours_after=25, refuse_unsafe_greeting=True)
    with caplog.at_level(logging.WARNING, logger="bridge.followup_discount"):
        assert asyncio.run(sweeper.run_once()) == 1
    # El nombre de la 194 pasa el filtro y sale; la 193 no se reserva.
    assert [claim["external_conversation_id"] for claim in supabase.claims] == [194]
    assert [sent["conversation_id"] for sent in chatwoot.sent] == [194]
    # No es una falla: se vuelve a contar en cada barrido.
    assert sweeper.last_scan_summary == "scanned=2 sent=1 greeting_name_refused=1"
    assert sweeper.last_scan_state == "healthy"
    assert (
        "conversation_followup_name_refused conversation=193 source=full_name"
        in caplog.text
    )
    assert phone_as_name not in caplog.text and "000 0193" not in caplog.text

    # Sin el filtro (Johanna) sale como hoy, con el nombre completo.
    chatwoot = chatwoot_with_a_phone_as_name()
    supabase = _FakeSupabase()
    sweeper = _sweeper(chatwoot, supabase, hours_after=25)
    assert asyncio.run(sweeper.run_once()) == 2
    greetings = {
        sent["conversation_id"]: sent["template_params"]["processed_params"]["body"]["1"]
        for sent in chatwoot.sent
    }
    assert greetings == {194: "Maria", 193: phone_as_name}


@pytest.mark.parametrize(
    ("contact_name", "greeting"),
    [
        # El push name no pasa el filtro (los parentesis), pero el saludo es
        # su primera palabra, que si pasa: sale. Un filtro sobre el nombre de
        # contacto lo rechazaria en cada barrido.
        ("Laura (mamá)", "Laura"),
        # Sin primer nombre el saludo cae al nombre completo, y este pasa el
        # filtro (los emoji son simbolos): sale entero. Un filtro sobre el
        # origen del saludo (full_name) lo rechazaria.
        ("✨Lucía✨", "✨Lucía✨"),
    ],
    ids=["unsafe_contact_safe_greeting", "safe_full_name"],
)
def test_the_name_filter_judges_the_greeting_that_would_go_out(
    contact_name: str, greeting: str
) -> None:
    # La 21 de ATT1 (F4) con el barredor de ATT1, que filtra el nombre. El
    # criterio es template_greeting_name_is_safe sobre el saludo resuelto, el
    # mismo del despachador: ni el nombre de contacto ni de donde salio.
    chatwoot = _att1_chatwoot()
    chatwoot.entries[0]["conversation"]["meta"]["sender"]["name"] = contact_name
    supabase = _FakeSupabase(issued_url=ATT1_AGENT_LINK)
    sweeper = _att1_sweeper(chatwoot, supabase, now=ATT1_LEAD_REPLIED_AT + 30 * HOUR)

    assert asyncio.run(sweeper.run_once()) == 1
    [sent] = chatwoot.sent
    assert sent["template_params"]["processed_params"]["body"]["1"] == greeting
    assert sent["content"].startswith(f"Hola, {greeting}. ")
    assert sweeper.last_scan_summary == "scanned=1 sent=1"


def test_the_portable_sweeper_claims_and_authorizes_with_the_resolved_identity() -> None:
    # La 21 de ATT1: Chatwoot trae el wa_id (521...) y el caso nacio del
    # formulario con 52.... La reserva y la autorizacion exigen la identidad
    # exacta: con la textual el cupon no saldria nunca.
    chatwoot = _att1_chatwoot()
    supabase = _FakeSupabase(issued_url=ATT1_AGENT_LINK)
    resolver = _StoredIdentity()
    sweeper = _att1_sweeper(
        chatwoot,
        supabase,
        now=ATT1_LEAD_REPLIED_AT + 30 * HOUR,
        external_user_id_resolver=resolver,
    )
    assert sweeper.only_phone is None

    assert asyncio.run(sweeper.run_once()) == 1
    # El mismo resolvedor del entrante, con el wa_id y la conversacion.
    assert resolver.calls == [("5215500000021", 21)]

    [claim] = supabase.claims
    assert claim["external_user_id"] == "525500000021"
    assert claim["phone_equivalence"] is True
    assert (claim["chatwoot_account_id"], claim["chatwoot_inbox_id"]) == (2, 11)
    assert claim["external_conversation_id"] == 21
    assert claim["command_key"] == "followup:21:2766"
    assert claim["regime"] == REGIME_LINK_SENT
    assert (claim["last_inbound_message_id"], claim["last_outbound_message_id"]) == (
        2765,
        2766,
    )
    assert (claim["template_name"], claim["template_language"], claim["coupon_code"]) == (
        ATT1_TEMPLATE,
        "es_MX",
        ATT1_COUPON,
    )

    # La autorizacion, con la misma identidad con que se reservo.
    [authorization] = supabase.authorizations
    assert authorization["external_user_id"] == "525500000021"
    assert authorization["issuance_id"] == "iss-21"
    assert authorization["chatwoot_conversation_id"] == 21
    assert authorization["trigger_external_message_id"] == "2766"

    # La plantilla de ATT1: dos parametros en el cuerpo y el cupon solo en el
    # boton, sobre el link que emitio la reserva.
    [sent] = chatwoot.sent
    assert sent["conversation_id"] == 21
    assert sent["command_key"] == "followup:21:2766"
    assert sent["template_params"]["name"] == ATT1_TEMPLATE
    assert sent["template_params"]["processed_params"] == {
        "body": {"1": "Lucia", "2": ATT1_PRODUCT},
        "buttons": [
            {
                "type": "url",
                "parameter": ATT1_AGENT_LINK.removeprefix("https://pay.hotmart.com/")
                + "&offDiscount=TIROIDES10",
            }
        ],
    }
    assert sent["content"].startswith("Hola, Lucia. ")
    assert ATT1_COUPON not in sent["content"]
    assert [f["status"] for f in supabase.finalized] == ["accepted_by_chatwoot"]
    assert [s["status"] for s in supabase.settled] == ["sent"]
    assert sweeper.last_scan_summary == "scanned=1 sent=1"


def test_without_a_manifest_the_claim_is_the_same_call_as_always() -> None:
    # Johanna: sin resolvedor ni equivalencia, la reserva recibe los mismos
    # argumentos de siempre (sin phone_equivalence) y la identidad textual.
    chatwoot = _FakeChatwoot(["194"])
    supabase = _FakeSupabase()
    sweeper = _sweeper(chatwoot, supabase, hours_after=25)
    assert (sweeper.only_phone, sweeper.send_hours) == (None, None)

    assert asyncio.run(sweeper.run_once()) == 1
    [claim] = supabase.claims
    assert set(claim) == {
        "external_conversation_id",
        "chatwoot_account_id",
        "chatwoot_inbox_id",
        "external_user_id",
        "contact_email",
        "command_key",
        "regime",
        "template_name",
        "template_language",
        "coupon_code",
        "last_inbound_message_id",
        "last_outbound_message_id",
        "inbound_age_seconds",
        "issuance_ulid",
    }
    assert claim["external_user_id"] == "593900000194"
    assert supabase.authorizations[0]["external_user_id"] == "593900000194"


def test_the_sweeper_tells_ready_its_silence_and_its_sender_scope() -> None:
    # Con manifiesto /ready publica cuanto tiene que llevar callado el lead
    # (abrir despues de la prueba se verifica con 86400) y si el entrante esta
    # acotado a un JID, sin el numero: los lee de aca.
    default = _sweeper(_FakeChatwoot(["194"]), _FakeSupabase())
    assert default.min_inbound_age_seconds == 86_400
    assert default.allowed_phone is None

    narrowed = _sweeper(
        _FakeChatwoot(["194"]),
        _FakeSupabase(),
        min_inbound_age_seconds=900,
        allowed_phone="593900000194",
    )
    assert narrowed.min_inbound_age_seconds == 900
    assert narrowed.allowed_phone == "593900000194"


async def _lookup_fails(wa_id: str, *, conversation_id: object) -> str:
    # Lo que levanta el resolvedor real cuando la base no contesta.
    raise RetryableChatwootWorkError("chatwoot_inbound_identity_lookup_failed")


async def _another_phone(wa_id: str, *, conversation_id: object) -> str:
    # Un resolvedor roto: devuelve un movil que no es otra forma del mismo.
    return "525500000099"


@pytest.mark.parametrize(
    ("resolver", "error_type"),
    [
        (_lookup_fails, "RetryableChatwootWorkError"),
        (_another_phone, "identity_not_equivalent"),
    ],
    ids=["lookup_raises", "another_phone"],
)
def test_an_identity_lookup_failure_reserves_nothing(
    resolver, error_type: str, caplog: pytest.LogCaptureFixture
) -> None:
    chatwoot = _att1_chatwoot()
    supabase = _FakeSupabase(issued_url=ATT1_AGENT_LINK)
    sweeper = _att1_sweeper(
        chatwoot,
        supabase,
        now=ATT1_LEAD_REPLIED_AT + 30 * HOUR,
        external_user_id_resolver=resolver,
    )
    with caplog.at_level(logging.WARNING, logger="bridge.followup_discount"):
        assert asyncio.run(sweeper.run_once()) == 0
    assert supabase.claims == []
    assert supabase.authorizations == []
    assert chatwoot.sent == []
    assert sweeper.last_scan_summary == "scanned=1 sent=0 identity_lookup_failed=1"
    assert sweeper.last_scan_state == "error"
    assert (
        "conversation_followup_identity_lookup_failed conversation=21 "
        f"error_type={error_type}"
    ) in caplog.text
    assert "5500000021" not in caplog.text and "5500000099" not in caplog.text


def test_blocked_not_template_reply_is_a_known_outcome() -> None:
    # Con manifiesto el cupon sale solo en una conversacion adoptada (la
    # respuesta a una plantilla nuestra): la RPC portable frena la que no lo
    # es. Es un veredicto de la base, no una falla del barrido.
    assert "blocked_not_template_reply" in FOLLOWUP_CLAIM_OUTCOMES
    chatwoot = _att1_chatwoot()
    supabase = _FakeSupabase(
        claim_outcome="blocked_not_template_reply", issued_url=ATT1_AGENT_LINK
    )
    sweeper = _att1_sweeper(chatwoot, supabase, now=ATT1_LEAD_REPLIED_AT + 30 * HOUR)

    assert asyncio.run(sweeper.run_once()) == 0
    [claim] = supabase.claims
    assert claim["phone_equivalence"] is True
    assert supabase.authorizations == []
    assert supabase.settled == []
    assert chatwoot.sent == []
    assert sweeper.last_scan_summary == "scanned=1 sent=0 blocked_not_template_reply=1"
    assert sweeper.last_scan_state == "healthy"


@pytest.mark.parametrize(
    "options",
    [
        {"only_phone": "5215500000021"},
        {"only_phone": "+52 55 0000 0021"},
        {"only_phone": ""},
        {"send_hours": (9, 21)},
        {"time_zone": MEXICO_CITY},
        {"send_hours": (21, 9), "time_zone": MEXICO_CITY},
        {"send_hours": (9, 9), "time_zone": MEXICO_CITY},
        {"send_hours": (-1, 21), "time_zone": MEXICO_CITY},
        {"send_hours": (9, 25), "time_zone": MEXICO_CITY},
        {"send_hours": (True, 21), "time_zone": MEXICO_CITY},
        {"send_hours": (9.0, 21), "time_zone": MEXICO_CITY},
        {"send_hours": [9, 21], "time_zone": MEXICO_CITY},
        {"send_hours": (9, 21, 0), "time_zone": MEXICO_CITY},
        {"send_hours": (9, 21), "time_zone": "America/Mexico_City"},
        {"refuse_unsafe_greeting": "yes"},
        {"phone_equivalence": 1},
        {"external_user_id_resolver": "resolve_inbound_external_user_id"},
    ],
    ids=[
        "phone_without_plus",
        "phone_with_spaces",
        "phone_empty",
        "hours_without_zone",
        "zone_without_hours",
        "hours_reversed",
        "hours_empty",
        "hours_negative",
        "hours_past_24",
        "hours_bool",
        "hours_float",
        "hours_list",
        "hours_three",
        "zone_as_text",
        "greeting_flag_not_bool",
        "equivalence_flag_not_bool",
        "resolver_not_callable",
    ],
)
def test_an_invalid_audience_or_schedule_never_builds_a_sweeper(options: dict) -> None:
    with pytest.raises(ValueError, match="invalid conversation followup configuration"):
        ConversationFollowupSweeper(
            chatwoot=object(),
            supabase=object(),
            account_id=2,
            inbox_id=11,
            template_name=ATT1_TEMPLATE,
            coupon_code=ATT1_COUPON,
            product_name=ATT1_PRODUCT,
            **options,
        )
