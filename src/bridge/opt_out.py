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
)


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


def _is_fully_quoted(text: str) -> bool:
    stripped = text.strip()
    for start, end in _QUOTE_PAIRS:
        if not stripped.startswith(start):
            continue
        remainder = stripped[len(start) :]
        closing_index = remainder.rfind(end)
        if closing_index <= 0:
            continue
        suffix = remainder[closing_index + len(end) :]
        if all(
            character.isspace()
            or unicodedata.category(character).startswith("P")
            for character in suffix
        ):
            return True
    return False


def detect_explicit_opt_out(messages: Sequence[object]) -> OptOutMatch | None:
    for index, message in enumerate(messages):
        if (
            not isinstance(message, str)
            or len(message) > MAX_MESSAGE_CHARS
            or _is_fully_quoted(message)
        ):
            continue
        normalized = _normalize(message)
        for rule_key, pattern in _RULES:
            if pattern.fullmatch(normalized):
                return OptOutMatch(rule_key=rule_key, message_index=index)
    return None
