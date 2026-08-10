import pytest

from bridge.opt_out import detect_explicit_opt_out


def test_detects_direct_do_not_write_again_request() -> None:
    match = detect_explicit_opt_out(["No me escriban más."])

    assert match is not None
    assert match.rule_key == "do_not_write_again"
    assert match.message_index == 0


@pytest.mark.parametrize(
    ("message", "rule_key"),
    [
        ("Por favor, no me contacten más. Gracias", "do_not_contact_again"),
        ("Dejen de contactarme", "stop_contacting"),
        ("Quiero darme de baja", "unsubscribe"),
        ("No quiero recibir más mensajes", "stop_receiving_messages"),
        ("¡Buenas! No me manden más mensajes, por favor 🙏", "do_not_message_again"),
    ],
)
def test_detects_only_approved_global_phrase_families(
    message: str, rule_key: str
) -> None:
    match = detect_explicit_opt_out([message])

    assert match is not None
    assert match.rule_key == rule_key


def test_ignores_messages_over_the_bounded_detector_limit() -> None:
    message = "No me escriban más" + (" " * 2_001)

    assert detect_explicit_opt_out([message]) is None


def test_scans_the_complete_canonical_batch() -> None:
    messages = (["ahora no"] * 50) + ["No me escriban más"]

    match = detect_explicit_opt_out(messages)

    assert match is not None
    assert match.message_index == 50


@pytest.mark.parametrize(
    "message",
    [
        "No quiero dejar de recibir mensajes",
        "Juan dijo: no me escriban más",
        "No gracias",
        "Ahora no",
        "Quiero darme de baja el precio",
        "No me escriban más porque estoy en una reunión",
        "¿Cómo hago para decir no me escriban más?",
    ],
)
def test_does_not_apply_global_opt_out_to_ambiguous_or_quoted_text(
    message: str,
) -> None:
    assert detect_explicit_opt_out([message]) is None


def test_returns_the_first_match_in_canonical_batch_order() -> None:
    match = detect_explicit_opt_out(
        ["Ahora no", "No quiero recibir más mensajes", "Quiero darme de baja"]
    )

    assert match is not None
    assert match.rule_key == "stop_receiving_messages"
    assert match.message_index == 1


@pytest.mark.parametrize(
    ("message", "rule_key"),
    [
        ("No me escribas más", "do_not_write_again"),
        ("No me contactes más", "do_not_contact_again"),
        ("No me envíes más mensajes", "do_not_message_again"),
        ("No quiero que me escriban más", "do_not_write_again"),
    ],
)
def test_detects_clear_singular_and_equivalent_global_phrases(
    message: str, rule_key: str
) -> None:
    match = detect_explicit_opt_out([message])

    assert match is not None
    assert match.rule_key == rule_key


@pytest.mark.parametrize(
    "message",
    [
        '"No me escriban más"',
        "«No me escriban más»",
        "“No me escriban más”",
        "‘No me escriban más’",
        "＂No me escriban más＂",
        '"No me escriban más".',
        "“No me escriban más”.",
    ],
)
def test_does_not_treat_a_fully_quoted_phrase_as_an_opt_out(message: str) -> None:
    assert detect_explicit_opt_out([message]) is None


@pytest.mark.parametrize("unsupported", [None, 123, {"text": "No me escriban más"}])
def test_ignores_unsupported_non_text_batch_entries(unsupported: object) -> None:
    assert detect_explicit_opt_out([unsupported]) is None
