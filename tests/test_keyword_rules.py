"""Las reglas de palabra clave (Z1 3.9, ``keyword-rules-v1``): el modulo.

Contrato: ``docs/contracts/keyword-rules-v1.md``.

Datos:

* El primer mensaje real con el texto prellenado de un link: la conversacion 455 de
  Maribel (inbox 13), capturada el 2026-11-26 de la API de Chatwoot de produccion y
  anonimizada adentro del contenedor
  (``chatwoot_conversation_messages_inbox_13_conv_455_keyword_20261126.json``). La
  persona abrio con «forever young» (la palabra clave publicada de la campana
  forever-young-2026 en ``maribel_db``) y mando otro mensaje enseguida: el primer
  turno del agente lleva los dos.
* La conversacion que abrio una plantilla nuestra: el primer contacto del inbox 11
  (ATT1, ``chatwoot_followup_candidate_inbox_11_20261010.json``), la misma que usa
  ``test_campaign_inbound_entry``.
* La historia normalizada es la que arma ``_normalize_chatwoot_history`` del bridge.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bridge.app import (
    CHATWOOT_CONVERSATION_RESET_COMMAND,
    CHATWOOT_CONVERSATION_RESET_CONFIRMATION,
    _normalize_chatwoot_history,
)
from bridge.campaign_inbound_entry import first_message_of_turn, normalize_entry_text
from bridge.keyword_rules import (
    NOT_SERVED_DRAFT,
    NOT_SERVED_RESERVED,
    NOT_SERVED_RULE,
    NOTE_KIND,
    NOTE_TEXT,
    KeywordRuleContext,
    KeywordRuleInvalid,
    KeywordRules,
    keyword_rules_setup,
    keywords_clash,
    macro_block,
    parse_rule,
    rule_blocks,
    wa_me_link,
)

FIXTURES = Path(__file__).parent / "fixtures"
MARIBEL_455 = json.loads(
    (FIXTURES / "chatwoot_conversation_messages_inbox_13_conv_455_keyword_20261126.json").read_text(encoding="utf-8")
)["conversation"]["messages"]
ATT1_FIRST_TOUCH = json.loads(
    (FIXTURES / "chatwoot_followup_candidate_inbox_11_20261010.json").read_text(encoding="utf-8")
)["conversation"]["messages"]
AGENT_BOT_MARIBEL = next(m["sender"]["id"] for m in MARIBEL_455 if (m.get("sender") or {}).get("type") == "agent_bot")
AGENT_BOT_ATT1 = ATT1_FIRST_TOUCH[0]["sender"]["id"]
RULE_ID = "6f1d2c34-1111-4a8e-9c1e-000000000001"
OTHER_ID = "6f1d2c34-1111-4a8e-9c1e-000000000002"


def _raw(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {"clave": "forever", "nombre": "Link Forever Young", "palabra_clave": "Forever Young",
                           "etiqueta": "link-forever", "macro_etiqueta": 91}
    raw.update(changes)
    return {key: value for key, value in raw.items() if value is not None}


def _row(rule_id: str = RULE_ID, **changes: Any) -> dict[str, Any]:
    return {"rule_id": rule_id, "rule_key": "x", "revision": 1, "rule": parse_rule(_raw(**changes)),
            "fingerprint": rule_id.replace("-", "")[:32] * 2, "approved_at": "2026-11-26T12:00:00Z"}


class _Labeler:
    def __init__(self, outcome: str = "added", *, error: Exception | None = None) -> None:
        self.calls: list[tuple[int, str]] = []
        self.outcome = outcome
        self.error = error

    async def ensure(self, conversation_id: int, *, source: str) -> str:
        if self.error is not None:
            raise self.error
        self.calls.append((conversation_id, source))
        return self.outcome


def _context(*, campaigns: tuple[str, ...] = (), labels: tuple[str, ...] = (),
             macros: frozenset[int] = frozenset({5})) -> KeywordRuleContext:
    return KeywordRuleContext(reserved_macros=macros, reserved_labels=lambda: labels,
                              campaign_keywords=lambda: campaigns)


def _rules(*rows: dict[str, Any], context: KeywordRuleContext | None = None,
           labeler: _Labeler | None = None) -> tuple[KeywordRules, _Labeler]:
    labeler = labeler or _Labeler()

    async def fetch() -> dict[str, Any]:
        return {"rules": list(rows)}

    live = KeywordRules(fetch=fetch, context=context or _context(), make_labeler=lambda _label, _macro: labeler,
                        reset_command=CHATWOOT_CONVERSATION_RESET_COMMAND,
                        reset_confirmation=CHATWOOT_CONVERSATION_RESET_CONFIRMATION)
    assert asyncio.run(live.refresh()) is True
    return live, labeler


def _turn(live: KeywordRules, history: list[dict[str, Any]], *, batch: list[int], agent_bot: int = AGENT_BOT_MARIBEL,
          complete: bool = True) -> Any:
    normalized = _normalize_chatwoot_history(history, agent_bot_id=agent_bot)
    return asyncio.run(live.turn(
        conversation_id=455, history=history, history_complete=complete, messages=normalized,
        batch_refs={str(message_id) for message_id in batch}, agent_replies_before=0))


# El primer turno real de la 455: los dos mensajes de la persona antes de cualquier respuesta.
FIRST_TURN = [m for m in MARIBEL_455 if m["id"] <= 7934]
FIRST_BATCH = [7933, 7934]


# ------------------------------------------------------------------ la forma


def test_a_rule_is_normalized_with_the_campaign_matching() -> None:
    rule = parse_rule(_raw(palabra_clave="  Hola,  quiero  SOPORTE técnico! "))

    assert rule == {"clave": "forever", "nombre": "Link Forever Young",
                    "palabra_clave": "Hola, quiero SOPORTE técnico!",
                    "palabra_normalizada": "hola quiero soporte tecnico", "etiqueta": "link-forever",
                    "macro_etiqueta": 91}
    assert rule["palabra_normalizada"] == normalize_entry_text(rule["palabra_clave"])


@pytest.mark.parametrize(
    ("changes", "code", "field"),
    [
        ({"flujo": {"producto": "p", "flujo": "f"}}, "keyword_rule_flow_not_supported", "flujo"),
        ({"clave": "Con Mayus"}, "keyword_rule_invalid", "clave"),
        ({"palabra_clave": "si"}, "keyword_rule_invalid", "palabra_clave"),
        ({"palabra_clave": "1234 5678"}, "keyword_rule_invalid", "palabra_clave"),
        ({"palabra_clave": "🖤🖤🖤"}, "keyword_rule_invalid", "palabra_clave"),
        ({"etiqueta": "Con Espacio"}, "keyword_rule_invalid", "etiqueta"),
        ({"macro_etiqueta": "91"}, "keyword_rule_invalid", "macro_etiqueta"),
        ({"macro_etiqueta": True}, "keyword_rule_invalid", "macro_etiqueta"),
        ({"nombre": None}, "keyword_rule_invalid", "nombre"),
    ],
)
def test_a_bad_rule_is_refused_with_its_field(changes: dict[str, Any], code: str, field: str) -> None:
    with pytest.raises(KeywordRuleInvalid) as raised:
        parse_rule(_raw(**changes))

    assert raised.value.code == code and raised.value.reason["field"] == field


def test_an_unknown_key_is_refused_without_its_value() -> None:
    with pytest.raises(KeywordRuleInvalid) as raised:
        parse_rule({**_raw(), "telefono": "+5215512345678"})

    assert raised.value.reason == {"field": "rule", "extra": ["telefono"]}


@pytest.mark.parametrize(
    ("first", "second", "clash"),
    [
        ("forever young", "forever young", True),
        ("forever young", "black pack forever young", True),  # una contiene a la otra
        ("soporte", "necesito soporte tecnico", True),
        ("soporte", "soportes", False),  # palabras enteras
        ("check in", "asistencia", False),
    ],
)
def test_two_keywords_clash_when_a_message_with_one_brings_the_other(first: str, second: str, clash: bool) -> None:
    assert keywords_clash(first, second) is clash and keywords_clash(second, first) is clash


def test_the_blocks_are_the_campaign_another_rule_and_what_the_product_uses() -> None:
    context = _context(campaigns=("Forever Young", "black pack"), labels=("forever-young-2026",), macros=frozenset({91}))

    blocks = rule_blocks(parse_rule(_raw(etiqueta="forever-young-2026")), context=context,
                         others=[(OTHER_ID, "young")])

    assert [block["code"] for block in blocks] == [
        "keyword_rule_clashes_with_campaign", "keyword_rule_clashes_with_rule", "keyword_rule_label_reserved",
        "keyword_rule_macro_reserved"]


def test_a_rule_does_not_clash_with_itself_and_cliente_or_automation_labels_are_reserved() -> None:
    assert rule_blocks(parse_rule(_raw()), context=_context(), others=[(RULE_ID, "forever young")],
                       rule_id=RULE_ID) == []
    for label in ("cliente", "automation_paused"):
        [block] = rule_blocks(parse_rule(_raw(etiqueta=label)), context=_context(), others=[])
        assert block["code"] == "keyword_rule_label_reserved"


def test_the_macro_has_to_add_exactly_the_label() -> None:
    assert macro_block([{"action_name": "add_label", "action_params": ["link-forever"]}], "link-forever") is None
    assert macro_block([{"action_name": "add_label", "action_params": ["link-forever"]},
                        {"action_name": "resolve_conversation", "action_params": []}], "link-forever") is not None


def test_the_wa_me_link_carries_the_text_as_written() -> None:
    assert wa_me_link("+52 1 55 1234 5678", "Hola, quiero soporte") == \
        "https://wa.me/5215512345678?text=Hola%2C%20quiero%20soporte"
    assert wa_me_link(None, "x") is None


# ------------------------------------------------------------------ lo que se sirve


def test_what_is_served_and_why_not() -> None:
    rows = [
        _row(RULE_ID),
        # Choca con la de arriba (aprobada antes): no se sirve.
        _row(OTHER_ID, clave="otra", palabra_clave="Black Pack Forever Young", etiqueta="otra"),
        # El macro es el de la baja: no se sirve.
        _row("6f1d2c34-1111-4a8e-9c1e-000000000003", clave="baja", palabra_clave="asistencia", macro_etiqueta=5),
        # La fila no tiene la forma: no se sirve.
        {"rule_id": "6f1d2c34-1111-4a8e-9c1e-000000000004", "rule": {"clave": "x"}, "fingerprint": "f"},
    ]
    live, _ = _rules(*rows)

    assert [rule.rule_id for rule in live.rules] == [RULE_ID]
    assert live.serving_state(RULE_ID) == "served"
    assert live.serving_state(OTHER_ID) == NOT_SERVED_RULE
    assert live.serving_state("6f1d2c34-1111-4a8e-9c1e-000000000003") == NOT_SERVED_RESERVED
    assert live.serving_state("6f1d2c34-1111-4a8e-9c1e-000000000004") == NOT_SERVED_DRAFT
    assert live.status_line() == "served=1,not_served=3,refresh=ok"


def test_a_failed_read_keeps_the_last_good_rules() -> None:
    calls = {"n": 0}

    async def fetch() -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("caida")
        return {"rules": [_row()]}

    live = KeywordRules(fetch=fetch, context=_context(), make_labeler=lambda *_: _Labeler(),
                        reset_command=CHATWOOT_CONVERSATION_RESET_COMMAND,
                        reset_confirmation=CHATWOOT_CONVERSATION_RESET_CONFIRMATION)
    assert asyncio.run(live.refresh()) is True
    assert asyncio.run(live.refresh()) is False

    assert len(live.rules) == 1 and live.status_line() == "served=1,not_served=0,refresh=failing:1"


def test_the_rule_of_a_message_is_the_one_whose_word_comes_first() -> None:
    live, _ = _rules(_row(RULE_ID, clave="a", palabra_clave="asistencia", etiqueta="a"),
                     _row(OTHER_ID, clave="b", palabra_clave="check in", etiqueta="b"))

    rule = live.match("Hola! Check-in para la asistencia de mañana")

    assert rule is not None and rule.key == "b"


# ------------------------------------------------------------------ el turno, sobre la captura real


def test_the_real_first_message_is_the_one_both_entries_look_at() -> None:
    normalized = _normalize_chatwoot_history(FIRST_TURN, agent_bot_id=AGENT_BOT_MARIBEL)

    first = first_message_of_turn(normalized, {str(ref) for ref in FIRST_BATCH})

    assert first is not None and first["text"] == "forever young"
    # El turno siguiente (otro mensaje de la persona, mas tarde) ya no tiene primer mensaje.
    assert first_message_of_turn(_normalize_chatwoot_history(MARIBEL_455, agent_bot_id=AGENT_BOT_MARIBEL),
                                 {"7940"}) is None


def test_the_real_first_message_gets_the_label_and_the_note(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bridge")
    live, labeler = _rules(_row())

    turn = _turn(live, FIRST_TURN, batch=FIRST_BATCH)

    assert turn.rule is not None and turn.rule.label == "link-forever"
    assert labeler.calls == [(455, "keyword_rule")]
    assert turn.note is not None and turn.note.kind == NOTE_KIND and turn.note.rendered == NOTE_TEXT
    assert turn.note.agent_replies_before == 0
    assert "no un\n" not in NOTE_TEXT and "No derives la conversación" in NOTE_TEXT
    assert "keyword_rule_label conversation=455 rule=forever label=link-forever outcome=added" in caplog.text
    assert "forever young" not in caplog.text  # el texto de la persona no va al log


def test_the_campaign_wins_over_a_rule_with_the_same_word(caplog: pytest.LogCaptureFixture) -> None:
    # Hoy en maribel_db, forever-young-2026 publicada con «forever young»: la campana gana (D-39.3).
    caplog.set_level(logging.INFO, logger="bridge")
    live, labeler = _rules(_row(), context=_context(campaigns=("forever young", "black pack")))

    turn = _turn(live, FIRST_TURN, batch=FIRST_BATCH)

    assert turn.rule is None and turn.note is None and labeler.calls == []
    assert live.serving_state(RULE_ID) == "choca_con_una_campana"
    assert "keyword_rule_skipped conversation=455 rule=forever reason=choca_con_una_campana" in caplog.text


def test_a_later_message_with_the_word_does_nothing() -> None:
    live, labeler = _rules(_row(palabra_clave="redactado"))

    # El 7940 dice «[mensaje del lead redactado]»: trae la palabra, pero no es el primer mensaje.
    turn = _turn(live, MARIBEL_455, batch=[7940])

    assert turn.rule is None and labeler.calls == []


def test_a_conversation_opened_by_our_template_gets_nothing() -> None:
    history = [dict(message) for message in ATT1_FIRST_TOUCH]
    first_reply = next(i for i, m in enumerate(history) if m.get("message_type") == 0)
    history[first_reply] = {**history[first_reply], "content": "forever young"}
    live, labeler = _rules(_row())

    turn = _turn(live, history[: first_reply + 1], batch=[history[first_reply]["id"]], agent_bot=AGENT_BOT_ATT1)

    assert turn.rule is None and labeler.calls == []


def test_an_incomplete_history_or_no_rules_gets_nothing() -> None:
    live, labeler = _rules(_row())
    assert _turn(live, FIRST_TURN, batch=FIRST_BATCH, complete=False).rule is None
    empty, _ = _rules()
    assert _turn(empty, FIRST_TURN, batch=FIRST_BATCH).rule is None
    assert labeler.calls == []


def test_a_failing_labeler_never_breaks_the_turn() -> None:
    live, _ = _rules(_row(), labeler=_Labeler(error=RuntimeError("chatwoot")))

    turn = _turn(live, FIRST_TURN, batch=FIRST_BATCH)

    assert turn.rule is None and turn.note is None


# ------------------------------------------------------------------ el arranque


def _settings(**changes: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "keyword_rules_enabled": True,
        "instance_manifest": SimpleNamespace(tenant_ref="lancemos", ally_ref="maribel", agent_segments=(),
                                             products=()),
        "chatwoot_account_id": 3, "chatwoot_inbox_id": 13,
        "automation_expert_change_enabled": True, "flow_approval_enabled": True,
        "chatwoot_opt_out_macro_id": 5,
    }
    values.update(changes)
    return SimpleNamespace(**values)


class _Base:
    async def get_live_keyword_rules(self, instance: dict[str, str]) -> dict[str, Any]:
        assert instance == {"tenant_ref": "lancemos", "ally_ref": "maribel"}
        return {"rules": [_row()]}


def _setup(settings: SimpleNamespace, *, control: object = object(), base: object = None,
           entry: object = None) -> KeywordRules | None:
    return keyword_rules_setup(settings, control, base if base is not None else _Base(), campaign_entry=entry,
                               reset_command=CHATWOOT_CONVERSATION_RESET_COMMAND,
                               reset_confirmation=CHATWOOT_CONVERSATION_RESET_CONFIRMATION)


def test_without_the_flag_nothing_is_built() -> None:
    assert _setup(_settings(keyword_rules_enabled=False)) is None
    assert _setup(SimpleNamespace()) is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"instance_manifest": None}, "manifest"),
        ({"chatwoot_inbox_id": None}, "CHATWOOT_ACCOUNT_ID"),
        ({"automation_expert_change_enabled": False}, "AUTOMATION_EXPERT_CHANGE_ENABLED"),
        ({"flow_approval_enabled": False}, "FLOW_APPROVAL_ENABLED"),
    ],
)
def test_the_flag_fails_closed(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _setup(_settings(**changes))


def test_the_flag_needs_chatwoot_control_and_the_database() -> None:
    with pytest.raises(ValueError, match="Chatwoot control"):
        _setup(_settings(), control=None)
    with pytest.raises(ValueError, match="Supabase"):
        _setup(_settings(), base=object())


def test_the_setup_reads_the_live_rules_and_knows_the_campaign_words_and_labels() -> None:
    entry = SimpleNamespace(segments=(SimpleNamespace(label="forever-young-2026", keywords=("forever young",)),))

    live = _setup(_settings(), entry=entry)

    assert live is not None
    assert asyncio.run(live.refresh()) is True
    assert live.serving_state(RULE_ID) == "choca_con_una_campana"
    assert list(live.context.campaign_keywords()) == ["forever young"]
    assert "forever-young-2026" in set(live.context.reserved_labels())
    assert 5 in live.context.reserved_macros
