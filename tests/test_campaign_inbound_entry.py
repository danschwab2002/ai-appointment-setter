"""La entrada a una campana por el primer mensaje (E-317, ``campaign-inbound-entry-v1``): el modulo.

Contrato: ``docs/contracts/campaign-inbound-entry-v1.md``. Diseno E-319 §1.1 y §1.2 (D4, D5,
D11, D14) y ``segment-labels-v1`` §3.

Datos:

* Quien abrio la conversacion se lee de historias capturadas de Chatwoot: la 6 del
  inbox 13 (Maribel, empieza la persona,
  ``chatwoot_conversation_messages_inbox_13_conv_6_20261029.json``), la 173 del
  inbox 9 (empieza la persona y despues hay actividades y una nota privada,
  ``chatwoot_conversation_messages_inbox_9_conv_173_20260926.json``) y la del
  primer contacto del inbox 11 (ATT1, empieza nuestra plantilla,
  ``chatwoot_followup_candidate_inbox_11_20261010.json``).
* La historia normalizada es la que arma ``_normalize_chatwoot_history`` del
  bridge sobre esas capturas, con el texto del primer mensaje reemplazado.
* **Deuda:** no hay captura de un primer mensaje real con el texto prellenado del
  boton de una landing de campana: «Black November» es un ejemplo.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bridge.app import (
    CHATWOOT_CONVERSATION_RESET_COMMAND,
    CHATWOOT_CONVERSATION_RESET_CONFIRMATION,
    _normalize_chatwoot_history,
)
from bridge.campaign_inbound_entry import (
    CAMPAIGN_ANSWER_KEY,
    NOTICE_KIND,
    CampaignEntry,
    CampaignEntryConfigError,
    EntrySegment,
    normalize_entry_text,
    opened_by_the_person,
    keyword_in,
    render_notice,
)
from bridge.instance_manifest import ManifestError, _agent_segments

FIXTURES = Path(__file__).parent / "fixtures"
MARIBEL_6 = json.loads(
    (FIXTURES / "chatwoot_conversation_messages_inbox_13_conv_6_20261029.json").read_text(encoding="utf-8")
)["payload"]
JOHANNA_173 = json.loads(
    (FIXTURES / "chatwoot_conversation_messages_inbox_9_conv_173_20260926.json").read_text(encoding="utf-8")
)["payload"]
ATT1_FIRST_TOUCH = json.loads(
    (FIXTURES / "chatwoot_followup_candidate_inbox_11_20261010.json").read_text(encoding="utf-8")
)["conversation"]["messages"]
AGENT_BOT_MARIBEL = MARIBEL_6[1]["sender"]["id"]
AGENT_BOT_ATT1 = ATT1_FIRST_TOUCH[0]["sender"]["id"]

KEY = "black-november"
LABEL = "black-november"
QUESTION = "¿Nos escribes por la promoción Black November?"
START = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
END = datetime(2026, 12, 1, 6, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)


def _segment(**changes: Any) -> EntrySegment:
    values: dict[str, Any] = {
        "key": KEY,
        "label": LABEL,
        "name": "Black November",
        "version": 1,
        "valid_from": START,
        "valid_until": END,
        "keywords": ("black november",),
        "question": QUESTION,
    }
    values.update(changes)
    return EntrySegment(**values)


class _Labeler:
    def __init__(self, outcome: str = "added") -> None:
        self.calls: list[tuple[int, str]] = []
        self.outcome = outcome

    async def ensure(self, conversation_id: int, *, source: str) -> str:
        self.calls.append((conversation_id, source))
        return self.outcome


class _Answers:
    def __init__(self, stored: dict[str, str] | None = None, *, fail: bool = False) -> None:
        self.stored = dict(stored or {})
        self.fail = fail
        self.reads: list[int] = []
        self.records: list[tuple[int, str, str]] = []

    async def get_segment_entry_answers(self, *, chatwoot_account_id: int, chatwoot_inbox_id: int,
                                        chatwoot_conversation_id: int) -> dict[str, str]:
        assert (chatwoot_account_id, chatwoot_inbox_id) == (2, 11)
        self.reads.append(chatwoot_conversation_id)
        if self.fail:
            raise RuntimeError("supabase down")
        return dict(self.stored)

    async def record_segment_entry_answer(self, *, chatwoot_account_id: int, chatwoot_inbox_id: int,
                                          chatwoot_conversation_id: int, segment_key: str, answer: str) -> str:
        self.records.append((chatwoot_conversation_id, segment_key, answer))
        if self.fail:
            raise RuntimeError("supabase down")
        if segment_key in self.stored:
            return "replayed"
        self.stored[segment_key] = answer
        return "recorded"


def _entry(*segments: EntrySegment, labeler: _Labeler | None = None, answers: _Answers | None = None,
           now: datetime = NOW) -> tuple[CampaignEntry, _Labeler, _Answers]:
    labeler = labeler or _Labeler()
    answers = answers if answers is not None else _Answers()
    segments = segments or (_segment(),)
    entry = CampaignEntry(
        segments=segments,
        labelers={segment.label: labeler for segment in segments},
        answers=answers,
        account_id=2,
        inbox_id=11,
        reset_command=CHATWOOT_CONVERSATION_RESET_COMMAND,
        reset_confirmation=CHATWOOT_CONVERSATION_RESET_CONFIRMATION,
        clock=lambda: now,
    )
    return entry, labeler, answers


def _inbound(first_text: str, *, created_at: datetime = NOW) -> list[dict[str, Any]]:
    """La historia de Maribel 6 (empieza la persona), cortada al primer mensaje, con otro texto y otra hora."""

    first = copy.deepcopy(MARIBEL_6[0])
    first["content"] = first_text
    first["created_at"] = int(created_at.timestamp())
    return [first]


def _run(entry: CampaignEntry, history: list[dict[str, Any]], *, batch: list[int] | None = None,
         labels: tuple[str, ...] = (), agent_bot: int = AGENT_BOT_MARIBEL, complete: bool = True,
         conversation: int = 9) -> Any:
    normalized = _normalize_chatwoot_history(history, agent_bot_id=agent_bot)
    refs = {str(message_id) for message_id in (batch or [history[-1]["id"]])}
    return asyncio.run(entry.turn(
        conversation_id=conversation,
        history=history,
        history_complete=complete,
        messages=normalized,
        batch_refs=refs,
        labels=labels,
        agent_replies_before=sum(1 for message in normalized if message["actor"] == "assistant"),
    ))


# ------------------------------------------------------------------ la palabra clave


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("Hola! Vengo por BLACK NOVEMBER 🖤", True),
        # La palabra clave en cualquier parte, con espacios de mas (D-E317-13): no es el texto del boton.
        ("Buenas tardes, me interesa la info de la promo.   Black    November", True),
        ("  black november", True),
        ("Quiero la promo black-november", True),
        ("Black Novémber, porfa", True),
        ("blacknovember", False),
        ("Hola, quiero informacion", False),
    ],
)
def test_the_keyword_is_whole_words_anywhere_without_accents_case_or_spaces(text: str, hit: bool) -> None:
    assert keyword_in(text, normalize_entry_text("Black November")) is hit


def test_a_first_message_with_the_keyword_gets_the_label_before_the_turn(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bridge")
    entry, labeler, answers = _entry()

    turn = _run(entry, _inbound("Hola, vengo por Black November"))

    assert turn.labels == (LABEL,) and turn.notice is None and turn.pending is None
    assert labeler.calls == [(9, "segment_entry_keyword")]
    assert answers.reads == []  # con la etiqueta puesta no hay nada que preguntar
    assert (
        f"segment_label_by_keyword conversation=9 segment={KEY} label={LABEL} outcome=added"
        in [record.getMessage() for record in caplog.records]
    )


def test_the_keyword_counts_only_in_the_first_message() -> None:
    """D14: quien escribio otra cosa primero entra por la pregunta, no por la palabra clave."""

    entry, labeler, _ = _entry(_segment(question=None))
    history = [*_inbound("Hola"), copy.deepcopy(MARIBEL_6[1])]
    second = copy.deepcopy(MARIBEL_6[0])
    second["id"] = MARIBEL_6[1]["id"] + 1
    second["content"] = "Black November"
    history.append(second)

    turn = _run(entry, history)

    assert turn.labels == () and labeler.calls == []


def test_the_label_is_remembered_while_the_macro_job_runs() -> None:
    entry, labeler, _ = _entry(_segment(question=None))
    first = _inbound("Black November")
    _run(entry, first)
    later = [*first, copy.deepcopy(MARIBEL_6[1])]
    reply = copy.deepcopy(MARIBEL_6[0])
    reply["id"] = MARIBEL_6[1]["id"] + 1
    reply["content"] = "¿Y cuanto sale?"
    later.append(reply)

    turn = _run(entry, later)

    assert turn.labels == (LABEL,)
    assert labeler.calls == [(9, "segment_entry_keyword")]  # una sola vez


@pytest.mark.parametrize("now", [START - timedelta(seconds=1), END])
def test_outside_its_window_nothing_happens(now: datetime) -> None:
    entry, labeler, answers = _entry(now=now)

    turn = _run(entry, _inbound("Black November", created_at=START))

    assert turn.labels == () and turn.notice is None and labeler.calls == [] and answers.reads == []


def test_with_an_incomplete_history_nothing_happens() -> None:
    entry, labeler, answers = _entry()

    turn = _run(entry, _inbound("Black November"), complete=False)

    assert turn.labels == () and turn.notice is None and labeler.calls == [] and answers.reads == []


def test_with_the_label_already_there_nothing_is_added() -> None:
    entry, labeler, answers = _entry()

    turn = _run(entry, _inbound("Black November"), labels=(LABEL,))

    assert turn.labels == () and turn.notice is None and labeler.calls == [] and answers.reads == []


# ------------------------------------------------------------------ la pregunta de entrada


def test_an_inbound_first_message_without_the_keyword_gets_the_notice() -> None:
    entry, labeler, answers = _entry()

    turn = _run(entry, _inbound("Hola, quiero informacion"))

    assert turn.labels == () and labeler.calls == []
    assert turn.pending == KEY and answers.reads == [9]
    assert turn.notice is not None and turn.notice.kind == NOTICE_KIND
    assert turn.notice.key == KEY and turn.notice.version == 1 and turn.notice.agent_replies_before == 0
    assert turn.notice.rendered == render_notice(_segment())
    assert QUESTION in turn.notice.rendered and f'"{CAMPAIGN_ANSWER_KEY}"' in turn.notice.rendered
    # E-322: el base v15 sin el bloque de la campana cotiza el precio de siempre; mientras se pregunta, no se cotiza.
    assert "no des precios, montos, descuentos ni plazos, ni los de siempre" in turn.notice.rendered
    # Corto y fijo (E-319 §1.2, ≈300 bytes mas la pregunta).
    assert len(turn.notice.rendered.encode("utf-8")) < 600


def test_the_notice_goes_on_the_next_turns_until_there_is_an_answer() -> None:
    entry, _, answers = _entry()
    history = [*_inbound("Hola"), copy.deepcopy(MARIBEL_6[1])]
    reply = copy.deepcopy(MARIBEL_6[0])
    reply["id"] = MARIBEL_6[1]["id"] + 1
    reply["content"] = "Mmm, ¿qué es eso?"
    history.append(reply)

    turn = _run(entry, history)

    assert turn.pending == KEY and turn.notice is not None and turn.notice.agent_replies_before == 1


@pytest.mark.parametrize(
    ("history", "agent_bot"),
    [
        # La abrio nuestra plantilla de primer contacto (ATT1): no es inbound.
        (copy.deepcopy(ATT1_FIRST_TOUCH[:2]), AGENT_BOT_ATT1),
    ],
)
def test_a_conversation_opened_by_our_template_gets_no_question(history: list[dict[str, Any]],
                                                                agent_bot: int) -> None:
    for message in history:
        message["created_at"] = int(NOW.timestamp())
    entry, _, answers = _entry()

    turn = _run(entry, history, agent_bot=agent_bot)

    assert turn.notice is None and turn.pending is None and answers.reads == []


def test_who_opened_the_conversation_is_read_from_the_captured_histories() -> None:
    kwargs = {"reset_command": CHATWOOT_CONVERSATION_RESET_COMMAND,
              "reset_confirmation": CHATWOOT_CONVERSATION_RESET_CONFIRMATION}
    assert opened_by_the_person(MARIBEL_6, **kwargs) is True
    assert opened_by_the_person(JOHANNA_173, **kwargs) is True
    assert opened_by_the_person(ATT1_FIRST_TOUCH, **kwargs) is False
    assert opened_by_the_person([], **kwargs) is False
    # Despues de /nuevo cuenta lo que vino despues (sin la confirmacion del bridge).
    reset = copy.deepcopy(ATT1_FIRST_TOUCH[1])
    reset["content"] = CHATWOOT_CONVERSATION_RESET_COMMAND
    confirmation = copy.deepcopy(ATT1_FIRST_TOUCH[0])
    confirmation["content"] = CHATWOOT_CONVERSATION_RESET_CONFIRMATION
    again = copy.deepcopy(ATT1_FIRST_TOUCH[1])
    assert opened_by_the_person([*ATT1_FIRST_TOUCH, reset, confirmation, again], **kwargs) is True


def test_a_conversation_that_started_before_the_campaign_gets_no_question() -> None:
    entry, _, answers = _entry()

    turn = _run(entry, _inbound("Hola", created_at=START - timedelta(days=2)))

    assert turn.notice is None and answers.reads == []


def test_a_no_in_the_database_is_not_asked_again() -> None:
    entry, _, answers = _entry(answers=_Answers({KEY: "no"}))

    turn = _run(entry, _inbound("Hola"))
    again = _run(entry, _inbound("Hola"))

    assert turn.notice is None and again.notice is None and turn.labels == ()
    assert answers.reads == [9]  # la segunda vez, de la memoria


def test_a_si_in_the_database_puts_the_label_on_the_turn() -> None:
    entry, labeler, _ = _entry(answers=_Answers({KEY: "si"}))

    turn = _run(entry, _inbound("Hola"))

    assert turn.notice is None and turn.labels == (LABEL,)
    assert labeler.calls == [(9, "segment_entry_question")]


def test_when_the_database_cannot_be_read_it_does_not_ask(caplog: pytest.LogCaptureFixture) -> None:
    entry, _, answers = _entry(answers=_Answers(fail=True))

    turn = _run(entry, _inbound("Hola"))

    assert turn.notice is None and turn.pending is None
    assert "segment_entry_answers_unavailable conversation=9 error=RuntimeError" in [
        record.getMessage() for record in caplog.records
    ]


def test_a_client_also_gets_the_question() -> None:
    """D11: a una clienta tambien se le pregunta."""

    entry, _, _ = _entry()

    turn = _run(entry, _inbound("Hola"), labels=("cliente",))

    assert turn.pending == KEY


# ------------------------------------------------------------------ la respuesta


def _settle(entry: CampaignEntry, turn: Any, answer: object) -> object:
    return asyncio.run(entry.settle(conversation_id=9, turn=turn, answer=answer))


def test_a_campaign_gone_while_the_model_was_thinking_does_not_break_the_turn(caplog: pytest.LogCaptureFixture) -> None:
    # La recarga del catalogo vivo (agent-campaigns-v1) saca el segmento entre el turno y la respuesta.
    caplog.set_level(logging.INFO, logger="bridge")
    entry, labeler, answers = _entry()
    turn = _run(entry, _inbound("Hola"))
    assert turn.pending == KEY

    entry.reload([], {})

    assert _settle(entry, turn, "si") is None
    assert answers.records == [] and labeler.calls == []
    assert f"segment_entry_answer_ignored conversation=9 segment={KEY} reason=segment_gone" in caplog.text


def test_a_reload_in_the_middle_of_the_answer_still_labels_who_said_yes() -> None:
    entry, labeler, answers = _entry()
    turn = _run(entry, _inbound("Hola"))
    recording = answers.record_segment_entry_answer

    async def record_and_reload(**kwargs: Any) -> str:
        entry.reload([], {})
        return await recording(**kwargs)

    answers.record_segment_entry_answer = record_and_reload  # type: ignore[method-assign]
    assert _settle(entry, turn, "si") == "si"
    assert labeler.calls == [(9, "segment_entry_question")]


def test_two_segments_with_the_same_keyword_are_refused_and_the_last_good_stays() -> None:
    first = _segment()
    entry, labeler, _ = _entry(first)
    twin = _segment(key="otra", label="otra", question=None)
    with pytest.raises(CampaignEntryConfigError, match="campaign_entry_keyword_repeated"):
        entry.reload([first, twin], {LABEL: labeler, "otra": labeler})
    assert entry.segments == (first,)


def test_a_si_puts_the_label_records_it_and_the_next_turn_has_the_segment(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bridge")
    entry, labeler, answers = _entry()
    turn = _run(entry, _inbound("Hola"))

    assert _settle(entry, turn, "si") == "si"

    assert answers.records == [(9, KEY, "si")]
    assert labeler.calls == [(9, "segment_entry_question")]
    assert (
        f"segment_entry_answer conversation=9 segment={KEY} answer=si outcome=recorded label=added"
        in [record.getMessage() for record in caplog.records]
    )
    following = _run(entry, _inbound("Hola"))
    assert following.labels == (LABEL,) and following.notice is None
    assert answers.reads == [9]  # el turno siguiente no vuelve a leer la base


def test_a_no_is_recorded_and_the_next_turn_does_not_ask() -> None:
    entry, labeler, answers = _entry()
    turn = _run(entry, _inbound("Hola"))

    assert _settle(entry, turn, "no") == "no"

    assert answers.records == [(9, KEY, "no")] and labeler.calls == []
    following = _run(entry, _inbound("Hola"))
    assert following.notice is None and following.labels == ()


@pytest.mark.parametrize("answer", [None, "tal vez", "SI", 1])
def test_without_a_clear_answer_nothing_is_recorded(answer: object) -> None:
    entry, labeler, answers = _entry()
    turn = _run(entry, _inbound("Hola"))

    assert _settle(entry, turn, answer) is None

    assert answers.records == [] and labeler.calls == []


def test_an_answer_on_a_turn_without_the_notice_is_ignored() -> None:
    entry, labeler, answers = _entry()
    turn = _run(entry, _inbound("Black November"))  # entro por la palabra clave

    assert _settle(entry, turn, "no") is None
    assert answers.records == [] and labeler.calls == [(9, "segment_entry_keyword")]


def test_a_si_that_cannot_be_recorded_still_puts_the_label(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bridge")
    answers = _Answers()
    entry, labeler, _ = _entry(answers=answers)
    turn = _run(entry, _inbound("Hola"))
    answers.fail = True

    assert _settle(entry, turn, "si") == "si"

    assert labeler.calls == [(9, "segment_entry_question")]
    assert any(
        record.getMessage().startswith(
            f"segment_entry_answer conversation=9 segment={KEY} answer=si outcome=record_failed:RuntimeError"
        )
        for record in caplog.records
    )


# ------------------------------------------------------------------ la configuracion


def test_two_questions_that_overlap_do_not_load() -> None:
    """D5: una sola campana con la pregunta prendida a la vez."""

    other = _segment(key="otra", label="otra", keywords=())
    with pytest.raises(CampaignEntryConfigError, match="two_questions_overlap"):
        _entry(_segment(), other)
    later = _segment(key="otra", label="otra", keywords=(), valid_from=END, valid_until=END + timedelta(days=5))
    entry, _, _ = _entry(_segment(), later)
    assert entry.has_question


def test_the_question_requires_the_database() -> None:
    with pytest.raises(CampaignEntryConfigError, match="requires_the_database"):
        CampaignEntry(segments=(_segment(),), labelers={LABEL: _Labeler()}, answers=None, account_id=2,
                      inbox_id=11, reset_command="/nuevo", reset_confirmation="ok")


def _declare(**extra: object) -> list[dict[str, object]]:
    return [{"clave": KEY, "etiqueta": LABEL, "conocimiento": "conocimiento/segmentos/black-november-v1.toml",
             "prioridad": 10, **extra}]


def test_the_manifest_reads_the_phrases_normalized_and_the_question() -> None:
    [segment] = _agent_segments(
        _declare(macro_etiqueta=41, palabras_clave=["Black November", "BLACK-friday"], pregunta_entrada=QUESTION),
        "conocimiento/knowledge-v1.toml",
        (),
    )

    assert segment.entry_keywords == ("black november", "black friday")
    assert segment.entry_question == QUESTION
    [plain] = _agent_segments(_declare(), "conocimiento/knowledge-v1.toml", ())
    assert plain.entry_keywords == () and plain.entry_question is None


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"palabras_clave": ["black"]}, "piden macro_etiqueta"),
        ({"pregunta_entrada": QUESTION}, "piden macro_etiqueta"),
        ({"macro_etiqueta": 41, "palabras_clave": []}, "de 1 a 8"),
        ({"macro_etiqueta": 41, "palabras_clave": ["a"] * 9}, "repetidos|de 1 a 8"),
        ({"macro_etiqueta": 41, "palabras_clave": ["🖤"]}, "letras o numeros"),
        ({"macro_etiqueta": 41, "palabras_clave": ["Black", "black"]}, "normalizan igual"),
        ({"macro_etiqueta": 41, "pregunta_entrada": "x" * 201}, "hasta 200"),
        ({"macro_etiqueta": 41, "palabra": "black"}, ""),
    ],
)
def test_the_manifest_rejects_a_bad_entry(extra: dict[str, object], message: str) -> None:
    with pytest.raises(ManifestError, match=message):
        _agent_segments(_declare(**extra), "conocimiento/knowledge-v1.toml", ())


def test_two_segments_with_the_same_phrase_do_not_load() -> None:
    raw = [
        *_declare(macro_etiqueta=41, palabras_clave=["black november"]),
        {"clave": "otra", "etiqueta": "otra", "conocimiento": "conocimiento/segmentos/otra-v1.toml",
         "prioridad": 20, "macro_etiqueta": 42, "palabras_clave": ["Black November"]},
    ]
    with pytest.raises(ManifestError, match="misma palabra clave"):
        _agent_segments(raw, "conocimiento/knowledge-v1.toml", ())
