from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OptOutMatch:
    rule_key: str
    message_index: int


_POLITE_PREFIX = r"(?:(?:por favor|hola|buenas|buen dia|buenos dias) )*"
_POLITE_SUFFIX = r"(?: (?:por favor|gracias))*"
MAX_MESSAGE_CHARS = 2_000
_QUOTE_PAIRS = (
    ("`", "`"),
    ("\"", "\""),
    ("'", "'"),
    ("«", "»"),
    ("‹", "›"),
    ("“", "”"),
    ("‘", "’"),
    ("„", "“"),
    ("＂", "＂"),
    ("「", "」"),
    ("『", "』"),
)
_RULES = (
    (
        "stop_receiving_messages",
        re.compile(_POLITE_PREFIX + r"no mas mensajes" + _POLITE_SUFFIX),
    ),
    (
        "do_not_write_again",
        re.compile(
            _POLITE_PREFIX
            + r"(?:no me (?:escriban|escribas) mas|no quiero que me (?:escriban|escribas) mas)"
            + _POLITE_SUFFIX
        ),
    ),
    (
        "do_not_contact_again",
        re.compile(
            _POLITE_PREFIX
            + r"(?:no me (?:contacten|contactes) mas|no quiero que me (?:contacten|contactes) mas)"
            + _POLITE_SUFFIX
        ),
    ),
    (
        "do_not_message_again",
        re.compile(
            _POLITE_PREFIX
            + r"no me (?:manden|mandes|envien|envies) mas mensajes"
            + _POLITE_SUFFIX
        ),
    ),
    (
        "stop_contacting",
        re.compile(_POLITE_PREFIX + r"dejen de contactarme" + _POLITE_SUFFIX),
    ),
    (
        "unsubscribe",
        re.compile(_POLITE_PREFIX + r"quiero darme de baja" + _POLITE_SUFFIX),
    ),
    (
        "stop_receiving_messages",
        re.compile(
            _POLITE_PREFIX + r"no quiero recibir mas mensajes" + _POLITE_SUFFIX
        ),
    ),
    # Added 2026-10-18 for the ATT1 cart touches (A2 and Z): the ways people
    # actually ask to stop, besides the button. Same shape as above: the whole
    # clause has to be the request, never a word inside other text.
    (
        "stop_keyword",
        re.compile(_POLITE_PREFIX + r"(?:stop|baja)" + _POLITE_SUFFIX),
    ),
    (
        "do_not_write_again",
        re.compile(
            _POLITE_PREFIX
            + r"(?:ya )?(?:no me (?:escriban|escribas|escriba)(?: mas)?"
            + r"|no quiero que me (?:escriban|escribas|escriba)(?: mas)?"
            + r"|(?:dejen|deja|deje) de escribirme)"
            + _POLITE_SUFFIX
        ),
    ),
    (
        "do_not_contact_again",
        re.compile(
            _POLITE_PREFIX
            + r"(?:ya )?(?:no me (?:contacten|contactes|contacte)(?: mas)?"
            + r"|(?:deja|deje) de contactarme)"
            + _POLITE_SUFFIX
        ),
    ),
    (
        "do_not_message_again",
        re.compile(
            _POLITE_PREFIX
            + r"(?:ya )?(?:no me (?:manden|mandes|mande|envien|envies|envie)"
            + r"(?: mas)? (?:mensajes|nada)"
            + r"|(?:dejen|deja|deje) de (?:mandarme|enviarme) (?:mensajes|cosas)"
            + r"|no quiero (?:recibir )?mas mensajes"
            + r"|basta de mensajes)"
            + _POLITE_SUFFIX
        ),
    ),
    (
        "unsubscribe",
        re.compile(
            _POLITE_PREFIX
            + r"(?:(?:quiero |quisiera )?darme de baja|denme de baja|me doy de baja"
            + r"|quiero la baja|(?:sacame|saquenme|eliminame|eliminenme|borrame|borrenme)"
            + r" de (?:la|su|tu) (?:lista|base))"
            + _POLITE_SUFFIX
        ),
    ),
)
# What may surround the request in the same message without changing it: a
# greeting, thanks, an apology or a plain lack of interest. Anything else (a
# reason, a time, a question) keeps the message from counting, because the
# opt-out cannot be undone (``20260809000100``).
_FILLER_WORDS = (
    r"(?:hola|buenas|buenos dias|buen dia|buenas tardes|buenas noches|gracias"
    r"|muchas gracias|mil gracias|por favor|porfa|saludos|disculpa|disculpe"
    r"|disculpen|perdon|bendiciones|que (?:tengas|tenga|tengan) (?:buen dia|buena tarde|buena noche)"
    r"|(?:ya )?no me interesa(?: mas)?|(?:ya )?no estoy interesad[oa]|no gracias|ok|okay|listo)"
)
_FILLER = re.compile(rf"(?:{_FILLER_WORDS}(?: {_FILLER_WORDS})*)?")
_CLAUSE_BREAK = re.compile(r"(?<=[.,;!?\n])")


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_marks = "".join(
        character
        for character in decomposed
        if unicodedata.category(character) != "Mn"
    )
    words_only = "".join(
        character if character.isalnum() else " " for character in without_marks
    )
    return re.sub(r"\s+", " ", words_only).strip()


def _is_quoted_or_referenced(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith(">"):
        return True
    for start, end in _QUOTE_PAIRS:
        if not stripped.startswith(start):
            continue
        remainder = stripped[len(start) :]
        closing_index = remainder.rfind(end)
        if closing_index <= 0:
            continue
        return True
    return False


def _clauses(text: str) -> list[str]:
    return [part for part in _CLAUSE_BREAK.split(text) if part.strip()]


def _is_question(clause: str) -> bool:
    return "?" in clause or "¿" in clause


def _match_rule(clause: str) -> str | None:
    normalized = _normalize(clause)
    for rule_key, pattern in _RULES:
        if pattern.fullmatch(normalized):
            return rule_key
    return None


def _match_message(message: str) -> str | None:
    """The rule a message asks for, or None.

    The message is split into clauses and counts only when one clause is the
    request, stated and not asked or quoted, and every other clause is filler:
    «Hola, no me interesa. No me escriban más, gracias» counts; «No me escriban
    más porque estoy en una reunión», «¿No más mensajes?» and «Hola. "No me
    escriban más"» do not.
    """
    found: str | None = None
    for clause in _clauses(message):
        if _is_quoted_or_referenced(clause):
            return None
        clause_rule = None if _is_question(clause) else _match_rule(clause)
        if clause_rule is not None:
            found = found or clause_rule
        elif _FILLER.fullmatch(_normalize(clause)) is None:
            return None
    return found


def detect_explicit_opt_out(messages: Sequence[object]) -> OptOutMatch | None:
    for index, message in enumerate(messages):
        if (
            not isinstance(message, str)
            or len(message) > MAX_MESSAGE_CHARS
            or _is_quoted_or_referenced(message)
        ):
            continue
        rule_key = _match_message(message)
        if rule_key is not None:
            return OptOutMatch(rule_key=rule_key, message_index=index)
    return None
