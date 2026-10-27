import pytest

from bridge.opt_out import detect_explicit_opt_out


def test_detects_direct_do_not_write_again_request() -> None:
    match = detect_explicit_opt_out(["No me escriban más."])

    assert match is not None
    assert match.rule_key == "do_not_write_again"
    assert match.message_index == 0


def test_detects_marcela_exact_no_more_messages_phrase() -> None:
    match = detect_explicit_opt_out(["No más mensajes"])

    assert match is not None
    assert match.rule_key == "stop_receiving_messages"
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
        "Juan dijo: no más mensajes",
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
        '"No más mensajes"',
        '`No más mensajes`',
        '> No más mensajes',
        '"No más mensajes" 🙄',
        "«No me escriban más»",
        "«No más mensajes»",
        "“No me escriban más”",
        "‘No me escriban más’",
        "＂No me escriban más＂",
        '"No me escriban más".',
        "“No me escriban más”.",
    ],
)
def test_does_not_treat_a_quoted_or_referenced_phrase_as_an_opt_out(
    message: str,
) -> None:
    assert detect_explicit_opt_out([message]) is None


@pytest.mark.parametrize("unsupported", [None, 123, {"text": "No me escriban más"}])
def test_ignores_unsupported_non_text_batch_entries(unsupported: object) -> None:
    assert detect_explicit_opt_out([unsupported]) is None


# Hueco 2 de la baja de ATT1 (18/10): con A2 y Z, quien quiere cortar lo
# escribe con sus palabras. Las frases salen de la lista de la sesion del 17/10
# (`documentacion/2026-10-17-flujos-comerciales-de-att1-lo-verificado-y-el-plan.md`).
@pytest.mark.parametrize(
    ("message", "rule_key"),
    [
        ("STOP", "stop_keyword"),
        ("Stop 🛑", "stop_keyword"),
        ("Baja", "stop_keyword"),
        ("Ya no me escriban", "do_not_write_again"),
        ("No me escriban", "do_not_write_again"),
        ("Dejen de escribirme por favor", "do_not_write_again"),
        ("Ya no me contacten", "do_not_contact_again"),
        ("No me manden nada", "do_not_message_again"),
        ("Dejen de mandarme mensajes", "do_not_message_again"),
        ("No quiero más mensajes", "do_not_message_again"),
        ("Basta de mensajes", "do_not_message_again"),
        ("Denme de baja", "unsubscribe"),
        ("Sáquenme de la lista", "unsubscribe"),
        ("Bórrenme de su base", "unsubscribe"),
    ],
)
def test_detects_the_ways_people_ask_to_stop(message: str, rule_key: str) -> None:
    match = detect_explicit_opt_out([message])
    assert match is not None
    assert match.rule_key == rule_key


@pytest.mark.parametrize(
    ("message", "rule_key"),
    [
        ("Hola, no me interesa. No me escriban más, gracias", "do_not_write_again"),
        ("Buenas tardes. Ya no me escriban por favor 🙏", "do_not_write_again"),
        ("No estoy interesada, denme de baja.", "unsubscribe"),
        ("Disculpen\nNo quiero recibir más mensajes", "stop_receiving_messages"),
        ("STOP. Gracias", "stop_keyword"),
    ],
)
def test_detects_the_request_inside_a_message_of_courtesy(
    message: str, rule_key: str
) -> None:
    match = detect_explicit_opt_out([message])
    assert match is not None
    assert match.rule_key == rule_key


@pytest.mark.parametrize(
    "message",
    [
        # Una razon o un momento: puede ser solo por ahora.
        "Estoy en una reunión, no me escriban",
        "No me escriban más hoy",
        "Ahora no, no me escriban hasta el lunes",
        "No me manden nada, ya compré",
        # Preguntas.
        "¿Si compro ya no me escriben más?",
        "Hola, ¿cómo me doy de baja?",
        "¿Stop?",
        # Citas y palabras sueltas dentro de otra frase.
        'Mi hija dice "No me escriban más"',
        'Hola. "No me escriban más".',
        "No pude pagar porque se dio de baja mi tarjeta",
        "La baja de precio vale hasta cuándo",
        "Stop, espera que busco la tarjeta",
        # Falta de interes sin pedir que corten: no es una baja.
        "Ya no me interesa, gracias",
        "No gracias",
    ],
)
def test_does_not_count_a_reason_a_question_or_a_word_inside_other_text(
    message: str,
) -> None:
    assert detect_explicit_opt_out([message]) is None


# La prueba del entrante de Maribel (27/10 12:31Z, v1.20.1): «no me hablen más»
# no registro ninguna baja y el agente contesto como si la hubiera registrado.
# Es el texto real de ese mensaje; las demas son sus variantes con «hablar» y
# «molestar» en tu, vos y ustedes.
def test_detects_the_real_maribel_message_of_2026_10_27() -> None:
    match = detect_explicit_opt_out(["no me hablen más"])

    assert match is not None
    assert match.rule_key == "do_not_talk_again"
    assert match.message_index == 0


@pytest.mark.parametrize(
    ("message", "rule_key"),
    [
        ("No me hablen más", "do_not_talk_again"),
        ("No me hablen mas", "do_not_talk_again"),
        ("No me hablen", "do_not_talk_again"),
        ("No me hables más", "do_not_talk_again"),
        ("No me hablés más", "do_not_talk_again"),
        ("No me hable más, por favor", "do_not_talk_again"),
        ("Ya no me hablen", "do_not_talk_again"),
        ("Dejen de hablarme", "do_not_talk_again"),
        ("Deja de hablarme", "do_not_talk_again"),
        ("Dejá de hablarme", "do_not_talk_again"),
        ("No quiero que me hablen", "do_not_talk_again"),
        ("No quiero que me hablen más", "do_not_talk_again"),
        ("No me molesten más", "do_not_bother_again"),
        ("No me molesten", "do_not_bother_again"),
        ("No me molestes más", "do_not_bother_again"),
        ("No me molestés más", "do_not_bother_again"),
        ("No me moleste más", "do_not_bother_again"),
        ("Dejen de molestarme", "do_not_bother_again"),
        ("Deja de molestarme", "do_not_bother_again"),
        ("Dejá de molestarme por favor", "do_not_bother_again"),
        ("No quiero que me molesten", "do_not_bother_again"),
        ("No quiero que me escriban", "do_not_write_again"),
        ("Hola, no me interesa. No me hablen más, gracias", "do_not_talk_again"),
    ],
)
def test_detects_hablar_and_molestar_opt_outs(message: str, rule_key: str) -> None:
    match = detect_explicit_opt_out([message])
    assert match is not None
    assert match.rule_key == rule_key


@pytest.mark.parametrize(
    "message",
    [
        # Cortar un tema no es cortar la conversacion.
        "No me hablen más de precios",
        "No me hables de cuotas",
        "¿Me pueden dejar de hablar de cuotas?",
        "Dejen de hablarme de la oferta",
        # Preguntas y citas.
        "¿No me van a hablar más?",
        "¿Me dejan de molestar?",
        'Mi esposo dice "no me molesten más"',
        "«No me hablen más»",
        # Una razon o un momento.
        "No me molesten, estoy en el trabajo",
        "No me hablen hasta el lunes",
        # Enojo o desinteres sin pedir que corten: no es una baja.
        "Basta",
        "Basta!",
        "No me interesa",
        "No me molesta",
    ],
)
def test_does_not_count_hablar_or_molestar_about_a_topic_a_question_or_a_quote(
    message: str,
) -> None:
    assert detect_explicit_opt_out([message]) is None
