"""Conocimiento comercial de una instancia: lo que el agente sabe de ese negocio.

Diseno: docs/design/setter-producto-instalable-v1.md §4.4 y el diseno del SOUL
generico del 26/09. Contrato: docs/contracts/commercial-knowledge-v1.md.

El SOUL es uno solo para todas las instancias (reglas, link de pago, derivacion).
Lo que cambia con el cliente vive en ``knowledge-v<N>.toml`` dentro del repo de la
instancia. El bridge lo carga al arrancar, lo valida, lo renderiza a markdown en un
orden fijo y lo manda como mensaje ``system`` en cada pedido a Hermes, que lo apila
sobre el SOUL del profile. Lo que no esta en este bloque no esta confirmado, y el
agente aplica la politica de derivacion.

El render es determinista: el mismo archivo produce el mismo texto y el mismo hash,
que es lo que permite saber con que conocimiento respondio el agente.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import re
import tomllib
from pathlib import Path
from typing import Any, Mapping

MAX_RENDERED_BYTES = 16 * 1024

STATUSES = ("borrador", "aprobado")

# Formas de credenciales que no pueden aparecer en un archivo que termina dentro
# del prompt. No pretende ser completo: es la ultima red, no la primera.
_SECRET_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bEAA[A-Za-z0-9]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),
)

_TOP_KEYS = {
    "cabecera",
    "identidad",
    "voz",
    "oferta",
    "contenido",
    "no_confirmado",
    "promesas_prohibidas",
    "limites_sensibles",
}
_TOP_OPTIONAL = {"faq"}


class KnowledgeError(ValueError):
    """El archivo de conocimiento no es valido. El mensaje dice que y donde."""


@dataclass(frozen=True)
class FaqEntry:
    question: str
    answer: str
    source: str
    valid_from: str


@dataclass(frozen=True)
class CommercialKnowledge:
    version: int
    status: str
    ally_ref: str
    approved_by: str | None
    approved_on: date | None
    speaks_for: str
    presentation: str
    brand: str
    language: str
    address_form: str
    audience: str | None
    assistant_name: str | None
    voice_summary: str
    voice_rules: tuple[str, ...]
    voice_avoid: tuple[str, ...]
    voice_examples: tuple[str, ...]
    offer_name: str
    offer_description: str
    offer_price: str
    offer_currency: str
    offer_guarantee: str | None
    offer_notes: tuple[str, ...]
    content_components: tuple[str, ...]
    content_statements: tuple[str, ...]
    unconfirmed: tuple[str, ...]
    forbidden_promises: tuple[str, ...]
    sensitive_vertical: str
    sensitive_rules: tuple[str, ...]
    faq: tuple[FaqEntry, ...]

    @property
    def approved(self) -> bool:
        return self.status == "aprobado"

    def render(self) -> str:
        """El bloque que recibe el agente, en un orden que no cambia."""

        lines = [
            f"# Conocimiento aprobado: {self.brand}",
            "",
            f"Version {self.version} del conocimiento de este negocio. Lo que no esta en este "
            "bloque no esta confirmado: no lo afirmes y aplica la politica de derivacion.",
            "",
            "## Identidad",
            "",
            f"- Hablas en nombre de: {self.speaks_for}.",
            f"- Te presentas como: {self.presentation}.",
            f"- Marca: {self.brand}.",
            f"- Idioma: {self.language}. Tratamiento: {self.address_form}.",
        ]
        if self.assistant_name:
            lines.append(f"- Nombre del asistente: {self.assistant_name}.")
        if self.audience:
            lines.append(f"- Audiencia: {self.audience}.")
        lines += ["", "## Voz", "", self.voice_summary, ""]
        lines += _numbered(self.voice_rules)
        if self.voice_avoid:
            lines += ["", "Evitar:", ""] + _bullets(self.voice_avoid)
        if self.voice_examples:
            lines += ["", "Ejemplos de estilo (ficticios, no son respuestas fijas):", ""]
            lines += _bullets(f"«{example}»" for example in self.voice_examples)
        lines += [
            "",
            "## Oferta",
            "",
            f"- Nombre: {self.offer_name}.",
            f"- Que es: {self.offer_description}",
            f"- Precio: {self.offer_currency} {self.offer_price}.",
            "- Garantia: "
            + (self.offer_guarantee if self.offer_guarantee else "no confirmada.")
        ]
        lines += _bullets(self.offer_notes)
        lines += ["", "## Contenido del programa", ""]
        if self.content_components:
            lines += ["Lista cerrada de componentes:", ""] + _bullets(self.content_components)
        else:
            lines.append(
                "No hay lista de componentes confirmada: si preguntan que incluye, "
                "cuantos modulos tiene o cuanto dura, deriva."
            )
        if self.content_statements:
            lines += ["", "Lo que si se puede decir:", ""] + _bullets(self.content_statements)
        lines += ["", "## No confirmado", "", "Si preguntan por esto, deriva:", ""]
        lines += _bullets(self.unconfirmed)
        lines += ["", "## Promesas prohibidas", ""] + _bullets(self.forbidden_promises)
        lines += [
            "",
            f"## Limites sensibles: {self.sensitive_vertical}",
            "",
        ] + _bullets(self.sensitive_rules)
        if self.faq:
            lines += ["", "## Preguntas frecuentes aprobadas", ""]
            for entry in self.faq:
                lines += [f"- P: {entry.question}", f"  R: {entry.answer}"]
        return "\n".join(lines).rstrip() + "\n"

    @property
    def rendered_sha256(self) -> str:
        return hashlib.sha256(self.render().encode("utf-8")).hexdigest()

    @classmethod
    def from_toml_file(cls, path: Path, *, require_approved: bool = True) -> CommercialKnowledge:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise KnowledgeError(f"no se pudo leer {path}") from exc
        try:
            payload = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise KnowledgeError(f"{path} no es TOML valido: {exc}") from exc
        return cls.from_mapping(payload, require_approved=require_approved)

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, Any], *, require_approved: bool = True
    ) -> CommercialKnowledge:
        _exact("conocimiento", payload, _TOP_KEYS, _TOP_OPTIONAL)

        header = _table(payload, "cabecera")
        _exact("cabecera", header, {"knowledge_version", "estado", "ally_ref"}, {"aprobado_por", "aprobado_el"})
        version = header["knowledge_version"]
        if type(version) is not int or version < 1:
            raise KnowledgeError("cabecera.knowledge_version debe ser un entero positivo")
        status = _text(header, "estado", "cabecera.estado")
        if status not in STATUSES:
            raise KnowledgeError(f"cabecera.estado debe ser uno de {', '.join(STATUSES)}")
        approved_by = _optional_text(header, "aprobado_por", "cabecera.aprobado_por")
        approved_on = header.get("aprobado_el")
        if approved_on is not None and (not isinstance(approved_on, date) or hasattr(approved_on, "hour")):
            raise KnowledgeError("cabecera.aprobado_el debe ser una fecha (2026-09-28)")
        if status == "aprobado" and (approved_by is None or approved_on is None):
            raise KnowledgeError("un conocimiento aprobado lleva aprobado_por y aprobado_el")
        if require_approved and status != "aprobado":
            raise KnowledgeError(
                "el conocimiento esta en borrador: el bridge solo carga uno aprobado"
            )

        identity = _table(payload, "identidad")
        _exact(
            "identidad",
            identity,
            {"habla_en_nombre_de", "presentacion", "marca", "idioma", "tratamiento"},
            {"audiencia", "asistente"},
        )
        voice = _table(payload, "voz")
        _exact("voz", voice, {"resumen", "reglas"}, {"evitar", "ejemplos"})
        offer = _table(payload, "oferta")
        _exact("oferta", offer, {"nombre", "descripcion", "precio", "moneda"}, {"garantia", "notas"})
        content = _table(payload, "contenido")
        _exact("contenido", content, {"componentes"}, {"se_puede_decir"})
        unconfirmed = _table(payload, "no_confirmado")
        _exact("no_confirmado", unconfirmed, {"items"})
        forbidden = _table(payload, "promesas_prohibidas")
        _exact("promesas_prohibidas", forbidden, {"items"})
        sensitive = _table(payload, "limites_sensibles")
        _exact("limites_sensibles", sensitive, {"vertical", "reglas"})

        faq_raw = payload.get("faq", [])
        if not isinstance(faq_raw, list):
            raise KnowledgeError("faq debe ser una lista de tablas [[faq]]")
        faq = []
        for index, raw in enumerate(faq_raw):
            where = f"faq[{index}]"
            if not isinstance(raw, dict):
                raise KnowledgeError(f"{where} debe ser una tabla")
            _exact(where, raw, {"pregunta", "respuesta", "fuente", "vigencia"})
            faq.append(
                FaqEntry(
                    question=_text(raw, "pregunta", f"{where}.pregunta"),
                    answer=_text(raw, "respuesta", f"{where}.respuesta"),
                    source=_text(raw, "fuente", f"{where}.fuente"),
                    valid_from=_text(raw, "vigencia", f"{where}.vigencia"),
                )
            )

        knowledge = cls(
            version=version,
            status=status,
            ally_ref=_text(header, "ally_ref", "cabecera.ally_ref"),
            approved_by=approved_by,
            approved_on=approved_on,
            speaks_for=_text(identity, "habla_en_nombre_de", "identidad.habla_en_nombre_de"),
            presentation=_text(identity, "presentacion", "identidad.presentacion"),
            brand=_text(identity, "marca", "identidad.marca"),
            language=_text(identity, "idioma", "identidad.idioma"),
            address_form=_text(identity, "tratamiento", "identidad.tratamiento"),
            audience=_optional_text(identity, "audiencia", "identidad.audiencia"),
            assistant_name=_optional_text(identity, "asistente", "identidad.asistente"),
            voice_summary=_text(voice, "resumen", "voz.resumen"),
            voice_rules=_list(voice, "reglas", "voz.reglas", allow_empty=False),
            voice_avoid=_list(voice, "evitar", "voz.evitar"),
            voice_examples=_list(voice, "ejemplos", "voz.ejemplos"),
            offer_name=_text(offer, "nombre", "oferta.nombre"),
            offer_description=_text(offer, "descripcion", "oferta.descripcion"),
            offer_price=_text(offer, "precio", "oferta.precio"),
            offer_currency=_text(offer, "moneda", "oferta.moneda"),
            offer_guarantee=_optional_text(offer, "garantia", "oferta.garantia"),
            offer_notes=_list(offer, "notas", "oferta.notas"),
            content_components=_list(content, "componentes", "contenido.componentes"),
            content_statements=_list(content, "se_puede_decir", "contenido.se_puede_decir"),
            unconfirmed=_list(unconfirmed, "items", "no_confirmado.items", allow_empty=False),
            forbidden_promises=_list(forbidden, "items", "promesas_prohibidas.items", allow_empty=False),
            sensitive_vertical=_text(sensitive, "vertical", "limites_sensibles.vertical"),
            sensitive_rules=_list(sensitive, "reglas", "limites_sensibles.reglas", allow_empty=False),
            faq=tuple(faq),
        )
        rendered = knowledge.render()
        size = len(rendered.encode("utf-8"))
        if size > MAX_RENDERED_BYTES:
            raise KnowledgeError(
                f"el bloque renderizado pesa {size} bytes; el maximo es {MAX_RENDERED_BYTES}"
            )
        for shape in _SECRET_SHAPES:
            if shape.search(rendered):
                raise KnowledgeError(
                    "el conocimiento contiene algo con forma de credencial; "
                    "los secretos no van en este archivo"
                )
        return knowledge


def _numbered(items: tuple[str, ...]) -> list[str]:
    return [f"{index}. {item}" for index, item in enumerate(items, start=1)]


def _bullets(items: Any) -> list[str]:
    return [f"- {item}" for item in items]


def _exact(where: str, table: Mapping[str, Any], required: set[str], optional: set[str] | None = None) -> None:
    optional = optional or set()
    missing = sorted(required - set(table))
    extra = sorted(set(table) - required - optional)
    if missing:
        raise KnowledgeError(f"{where}: faltan {', '.join(missing)}")
    if extra:
        raise KnowledgeError(f"{where}: claves no soportadas {', '.join(extra)}")


def _table(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise KnowledgeError(f"{key} debe ser una tabla")
    return value


def _text(table: Mapping[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeError(f"{where} debe ser un texto no vacio")
    return value.strip()


def _optional_text(table: Mapping[str, Any], key: str, where: str) -> str | None:
    if key not in table:
        return None
    return _text(table, key, where)


def _list(table: Mapping[str, Any], key: str, where: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise KnowledgeError(f"{where} debe ser una lista de textos no vacios")
    if not allow_empty and not value:
        raise KnowledgeError(f"{where} no puede estar vacia")
    return tuple(item.strip() for item in value)
