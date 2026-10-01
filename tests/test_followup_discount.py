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
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bridge.chatwoot import ChatwootProtocolError
from bridge.followup_discount import (
    REGIME_LINK_SENT,
    REGIME_PAYMENT_FAILED,
    REGIME_WENT_QUIET,
    ConversationFollowupSweeper,
    evaluate_followup_candidate,
    followup_button_suffix,
    parse_followup_template,
)
from bridge.reactivation import evaluate_reactivation_candidate


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
TEMPLATE = "johanna_seguimiento_descuento_01"
COUPON = "JOHANNA10"
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
    ],
)
def test_a_template_that_is_not_exactly_ours_fails_closed(mutate, error: str) -> None:
    payload = copy.deepcopy(DOCUMENTED)
    mutate(payload["message_templates"][0])
    with pytest.raises(ChatwootProtocolError, match=error):
        parse_followup_template(payload, template_name=TEMPLATE)


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
    def __init__(self, conversations: list[str], *, fail_send: bool = False) -> None:
        self.entries = []
        for conversation_id in conversations:
            details, messages = _conversation(conversation_id)
            self.entries.append({"conversation": details, "messages": messages})
        self.fail_send = fail_send
        self.sent: list[dict] = []
        self.overrides: dict[int, list[dict]] = {}
        self.listings: list[dict] = []

    async def get_inbox(self, *, inbox_id: int) -> dict:
        return copy.deepcopy(DOCUMENTED)

    async def list_recent_conversations_with_messages(self, **kwargs: object) -> list[dict]:
        self.listings.append(kwargs)
        return copy.deepcopy(self.entries)

    async def get_conversation_messages(self, *, conversation_id: int, limit: int) -> list[dict]:
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
    def __init__(self, claim_outcome: str = "claimed") -> None:
        self.claim_outcome = claim_outcome
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
            checkout_url_final=ISSUED_URL if claimed else None,
            sck_value="hermes|v1|01K5ABCDEFX2VYB4M6X9CDPTF1" if claimed else None,
        )

    async def authorize_chatwoot_checkout_issuance_v2(self, **kwargs: object) -> SimpleNamespace:
        self.authorizations.append(kwargs)
        return SimpleNamespace(outcome="request_started")

    async def finalize_chatwoot_checkout_issuance_v2(self, **kwargs: object) -> None:
        self.finalized.append(kwargs)

    async def settle_conversation_followup(self, **kwargs: object) -> None:
        self.settled.append(kwargs)


def _sweeper(chatwoot: _FakeChatwoot, supabase: _FakeSupabase, *, hours_after: float = 30):
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
