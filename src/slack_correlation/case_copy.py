"""Closed commercial language for operator correlation cases."""

from __future__ import annotations

from dataclasses import dataclass


SUPPORTED_EVENT_TYPES = frozenset(
    {
        "PURCHASE_APPROVED",
        "PURCHASE_OUT_OF_SHOPPING_CART",
        "PURCHASE_CANCELED",
    }
)
SUPPORTED_OUTCOMES = frozenset({"unmatched", "ambiguous", "conflict"})

EVENT_OUTCOME_CODES = {
    ("PURCHASE_APPROVED", "unmatched"): "COR-001",
    ("PURCHASE_APPROVED", "ambiguous"): "COR-002",
    ("PURCHASE_APPROVED", "conflict"): "COR-003",
    ("PURCHASE_OUT_OF_SHOPPING_CART", "unmatched"): "COR-010",
    ("PURCHASE_OUT_OF_SHOPPING_CART", "ambiguous"): "COR-011",
    ("PURCHASE_OUT_OF_SHOPPING_CART", "conflict"): "COR-012",
    ("PURCHASE_CANCELED", "unmatched"): "COR-013",
    ("PURCHASE_CANCELED", "ambiguous"): "COR-014",
    ("PURCHASE_CANCELED", "conflict"): "COR-015",
}
CODE_TO_EVENT_OUTCOME = {code: key for key, code in EVENT_OUTCOME_CODES.items()}


@dataclass(frozen=True)
class CommercialCaseCopy:
    event_type: str
    observed_label: str
    situation: str
    task: str
    impact: str
    button: str
    modal_title: str
    singular_question: str
    multiple_question: str
    no_candidate_question: str
    identity_email_label: str
    identity_phone_label: str

    def title(self, outcome: str) -> str:
        suffix = {
            "unmatched": {
                "PURCHASE_APPROVED": "no encontramos al comprador",
                "PURCHASE_OUT_OF_SHOPPING_CART": "no encontramos a la persona",
                "PURCHASE_CANCELED": "no encontramos a la persona",
            },
            "ambiguous": {
                "PURCHASE_APPROVED": "varios compradores posibles",
                "PURCHASE_OUT_OF_SHOPPING_CART": "varias personas posibles",
                "PURCHASE_CANCELED": "varias personas posibles",
            },
            "conflict": {
                "PURCHASE_APPROVED": "datos de personas diferentes",
                "PURCHASE_OUT_OF_SHOPPING_CART": "identidad contradictoria",
                "PURCHASE_CANCELED": "identidad contradictoria",
            },
        }.get(outcome)
        if suffix is None:
            raise ValueError("unsupported_correlation_outcome")
        prefix = {
            "PURCHASE_APPROVED": "Compra confirmada",
            "PURCHASE_OUT_OF_SHOPPING_CART": "Checkout abandonado",
            "PURCHASE_CANCELED": "Pago no completado",
        }[self.event_type]
        return f"{prefix}: {suffix[self.event_type]}"

    def problem(self, outcome: str) -> str:
        if outcome == "unmatched":
            return "No encontramos ningún registro previo compatible con los datos recibidos."
        if outcome == "ambiguous":
            return "Encontramos más de una persona posible y el sistema no puede elegir una con seguridad."
        if outcome == "conflict":
            return "El email coincide con una persona y el teléfono con otra."
        raise ValueError("unsupported_correlation_outcome")


_EVENT_COPY = {
    "PURCHASE_APPROVED": CommercialCaseCopy(
        event_type="PURCHASE_APPROVED",
        observed_label="Compra confirmada por Hotmart",
        situation="Hotmart confirmó una compra.",
        task="determinar quién realizó la compra utilizando una fuente adicional confiable.",
        impact="las personas posibles quedan fuera de recuperación para evitar contactar a quien ya compró.",
        button="Identificar comprador",
        modal_title="Identificar comprador",
        singular_question="¿Esta compra pertenece a esta persona?",
        multiple_question="¿A cuál persona pertenece esta compra?",
        no_candidate_question="No encontramos al comprador. ¿Qué querés hacer?",
        identity_email_label="Email informado en la compra",
        identity_phone_label="Teléfono informado en la compra",
    ),
    "PURCHASE_OUT_OF_SHOPPING_CART": CommercialCaseCopy(
        event_type="PURCHASE_OUT_OF_SHOPPING_CART",
        observed_label="Checkout abandonado informado por Hotmart",
        situation="Hotmart informó una salida del checkout sin compra confirmada.",
        task="identificar quién inició este checkout antes de comenzar una recuperación.",
        impact="no se iniciará recuperación para ninguna de las personas posibles.",
        button="Identificar intento abandonado",
        modal_title="Identificar abandono",
        singular_question="¿Esta persona inició el checkout?",
        multiple_question="¿Cuál persona inició este checkout?",
        no_candidate_question="No encontramos a la persona. ¿Qué querés hacer?",
        identity_email_label="Email informado en el checkout",
        identity_phone_label="Teléfono informado en el checkout",
    ),
    "PURCHASE_CANCELED": CommercialCaseCopy(
        event_type="PURCHASE_CANCELED",
        observed_label="Pago no completado informado por Hotmart",
        situation="Hotmart informó un intento de pago no completado.",
        task="identificar quién intentó realizar el pago antes de ofrecer asistencia o recuperación.",
        impact="no se enviará ningún mensaje de asistencia o recuperación.",
        button="Identificar intento de pago",
        modal_title="Identificar pago",
        singular_question="¿Esta persona intentó realizar el pago?",
        multiple_question="¿Cuál persona intentó realizar el pago?",
        no_candidate_question="No encontramos a la persona. ¿Qué querés hacer?",
        identity_email_label="Email informado en el pago",
        identity_phone_label="Teléfono informado en el pago",
    ),
}


def commercial_case_copy(event_type: object) -> CommercialCaseCopy:
    if not isinstance(event_type, str) or event_type not in _EVENT_COPY:
        raise ValueError("unsupported_correlation_event_type")
    return _EVENT_COPY[event_type]


def operator_task(copy: CommercialCaseCopy, outcome: str) -> str:
    if outcome != "unmatched":
        return copy.task
    return (
        "Investigá la identidad en una fuente externa autorizada. Esta tarjeta es "
        "informativa porque todavía no existe una persona candidata para elegir."
    )
