"""Conocimiento comercial de una instancia: lo que el agente sabe de ese negocio.

Diseno: docs/design/setter-producto-instalable-v1.md §4.4 y el diseno del SOUL
generico del 26/09. Contrato: docs/contracts/commercial-knowledge-v1.md.

El SOUL es uno solo para todas las instancias (reglas, link de pago, derivacion).
Lo que cambia con el cliente vive en ``knowledge-v<N>.toml`` dentro del repo de la
instancia. El bridge lo carga al arrancar, lo valida, lo renderiza a markdown en un
orden fijo y lo manda como mensaje ``system`` en cada pedido a Hermes, que lo apila
sobre el SOUL del profile. Lo que no esta en este bloque no esta confirmado, y el
agente aplica la politica de derivacion.

El archivo tiene dos formas. La de un solo producto pone la oferta y el contenido
arriba, con todo lo demas. La de varios productos (desde el 2026-10-16) pone arriba
lo comun al negocio y declara cada producto en ``[[productos]]``, con su oferta, su
contenido y lo que es solo suyo. El agente recibe todo y deduce de que producto se
habla. Un archivo de la primera forma se renderiza igual que antes, byte a byte, y
su hash no cambia.

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

# Hasta la 1.8.0 eran 20.000 bytes, por debajo de los 20.000 caracteres que Hermes
# deja del SOUL. El 2026-10-16 se leyo en la imagen de produccion (v2026.8.31) que
# el ``system`` de cada pedido no pasa por ese recorte: api_server.py junta los
# mensajes system en ``ephemeral_system_prompt`` y conversation_loop.py lo suma al
# prompt sin ``_truncate_content``, que solo corta los archivos de contexto. El
# tope de ahora es por costo y por atencion, porque cada turno lo paga entero: dos
# productos de ATT1, estimados sobre el v7, dan unos 26 KB.
MAX_RENDERED_BYTES = 32_000

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

# La forma de un solo producto.
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
_TOP_OPTIONAL = {"faq", "pagos"}

# La forma de varios productos: arriba lo comun, y cada producto en [[productos]].
_TOP_KEYS_BY_PRODUCT = {
    "cabecera",
    "identidad",
    "voz",
    "no_confirmado",
    "promesas_prohibidas",
    "limites_sensibles",
    "productos",
}
_TOP_OPTIONAL_BY_PRODUCT = {"faq", "pagos", "se_puede_decir"}
_PRODUCT_KEYS = {"clave", "oferta", "contenido"}
_PRODUCT_OPTIONAL = {"no_confirmado", "promesas_prohibidas", "faq"}

# El mismo formato que las referencias del manifiesto de la instancia.
_PRODUCT_REF = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")


class KnowledgeError(ValueError):
    """El archivo de conocimiento no es valido. El mensaje dice que y donde."""


@dataclass(frozen=True)
class FaqEntry:
    question: str
    answer: str
    source: str
    valid_from: str


@dataclass(frozen=True)
class CountryPaymentNames:
    """Como se llaman la transferencia y el efectivo en un pais."""

    country: str
    transfer: str | None
    cash: str | None


@dataclass(frozen=True)
class PaymentSteps:
    method: str
    steps: tuple[str, ...]


@dataclass(frozen=True)
class RefusalGuide:
    """Que hacer cuando el medio de pago rechaza por un motivo."""

    reason: str
    arrives_as: tuple[str, ...]
    what_to_do: str


@dataclass(frozen=True)
class PaymentGuide:
    """La seccion [pagos]: lo que el agente usa ante una compra fallida.

    Todo lo declara la instancia. El producto no nombra ningun medio local
    (SPEI, OXXO, PSE): cambian con el pais y con lo que tiene prendido cada
    oferta en la plataforma de pago.
    """

    main_country: str
    methods: tuple[str, ...]
    by_country: tuple[CountryPaymentNames, ...]
    steps: tuple[PaymentSteps, ...]
    refusals: tuple[RefusalGuide, ...]
    rules: tuple[str, ...]


@dataclass(frozen=True)
class ProductKnowledge:
    """Un producto del negocio: su oferta, su contenido y lo que es solo suyo.

    En la forma de un solo producto, ``key`` es None y las listas propias quedan
    vacias: el no confirmado, las promesas y las preguntas van todas arriba.
    """

    key: str | None
    offer_name: str
    offer_description: str
    offer_price: str
    offer_currency: str
    offer_guarantee: str | None
    offer_notes: tuple[str, ...]
    content_components: tuple[str, ...]
    content_statements: tuple[str, ...]
    unconfirmed: tuple[str, ...] = ()
    forbidden_promises: tuple[str, ...] = ()
    faq: tuple[FaqEntry, ...] = ()


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
    unconfirmed: tuple[str, ...]
    forbidden_promises: tuple[str, ...]
    sensitive_vertical: str
    sensitive_rules: tuple[str, ...]
    faq: tuple[FaqEntry, ...]
    products: tuple[ProductKnowledge, ...]
    payments: PaymentGuide | None = None
    statements: tuple[str, ...] = ()
    by_product: bool = False

    @property
    def approved(self) -> bool:
        return self.status == "aprobado"

    def render(self) -> str:
        """El bloque que recibe el agente, en un orden que no cambia."""

        if self.by_product:
            return self._render_by_product()
        return self._render_single()

    def _render_single(self) -> str:
        # La forma de un solo producto. No tocar: el hash de los conocimientos
        # aprobados que ya corren sale de este texto.
        product = self.products[0]
        lines = self._render_title()
        lines += self._render_identity_and_voice()
        lines += _render_offer(product)
        if self.payments is not None:
            lines += _render_payments(self.payments)
        lines += _render_content(product)
        lines += ["", "## No confirmado", "", "Si preguntan por esto, deriva:", ""]
        lines += _bullets(self.unconfirmed)
        lines += ["", "## Promesas prohibidas", ""] + _bullets(self.forbidden_promises)
        lines += self._render_sensitive()
        lines += _render_faq(self.faq)
        return "\n".join(lines).rstrip() + "\n"

    def _render_by_product(self) -> str:
        names = [product.offer_name for product in self.products]
        lines = self._render_title()
        if len(names) == 1:
            lines += [
                "",
                "Primero va lo comun al negocio. Despues, la seccion de su producto, "
                f"{names[0]}, con lo que es solo de ese producto.",
            ]
        else:
            lines += [
                "",
                f"Primero va lo comun al negocio. Despues, una seccion por cada uno de sus "
                f"{len(names)} productos ({_join(names)}), con lo que es solo de ese producto: "
                "no lo apliques a los otros.",
            ]
        lines += self._render_identity_and_voice()
        if self.payments is not None:
            lines += _render_payments(self.payments)
        if self.statements:
            lines += ["", "## Lo que si se puede decir", ""] + _bullets(self.statements)
        lines += ["", "## No confirmado", "", "Si preguntan por esto, deriva:", ""]
        lines += _bullets(self.unconfirmed)
        lines += ["", "## Promesas prohibidas", ""] + _bullets(self.forbidden_promises)
        lines += self._render_sensitive()
        lines += _render_faq(self.faq)
        for product in self.products:
            lines += ["", f"# Producto: {product.offer_name}"]
            lines += _render_offer(product)
            lines += _render_content(product)
            if product.unconfirmed:
                lines += ["", "## No confirmado", "", "Si preguntan por esto, deriva:", ""]
                lines += _bullets(product.unconfirmed)
            if product.forbidden_promises:
                lines += ["", "## Promesas prohibidas", ""] + _bullets(product.forbidden_promises)
            lines += _render_faq(product.faq)
        return "\n".join(lines).rstrip() + "\n"

    def _render_title(self) -> list[str]:
        return [
            f"# Conocimiento aprobado: {self.brand}",
            "",
            f"Version {self.version} del conocimiento de este negocio. Lo que no esta en este "
            "bloque no esta confirmado: no lo afirmes y aplica la politica de derivacion.",
        ]

    def _render_identity_and_voice(self) -> list[str]:
        lines = [
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
        return lines

    def _render_sensitive(self) -> list[str]:
        return [
            "",
            f"## Limites sensibles: {self.sensitive_vertical}",
            "",
        ] + _bullets(self.sensitive_rules)

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
        by_product = "productos" in payload
        if by_product:
            for key in ("oferta", "contenido"):
                if key in payload:
                    raise KnowledgeError(
                        f"con [[productos]], {key} va adentro de cada producto, no arriba"
                    )
            _exact("conocimiento", payload, _TOP_KEYS_BY_PRODUCT, _TOP_OPTIONAL_BY_PRODUCT)
        else:
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
        unconfirmed = _table(payload, "no_confirmado")
        _exact("no_confirmado", unconfirmed, {"items"})
        forbidden = _table(payload, "promesas_prohibidas")
        _exact("promesas_prohibidas", forbidden, {"items"})
        sensitive = _table(payload, "limites_sensibles")
        _exact("limites_sensibles", sensitive, {"vertical", "reglas"})

        statements: tuple[str, ...] = ()
        if by_product:
            products = _products(payload["productos"])
            if "se_puede_decir" in payload:
                said = _table(payload, "se_puede_decir")
                _exact("se_puede_decir", said, {"items"})
                statements = _list(said, "items", "se_puede_decir.items", allow_empty=False)
        else:
            products = (_product(payload, key=None, where=""),)

        payments = _payments(payload["pagos"]) if "pagos" in payload else None

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
            unconfirmed=_list(unconfirmed, "items", "no_confirmado.items", allow_empty=False),
            forbidden_promises=_list(forbidden, "items", "promesas_prohibidas.items", allow_empty=False),
            sensitive_vertical=_text(sensitive, "vertical", "limites_sensibles.vertical"),
            sensitive_rules=_list(sensitive, "reglas", "limites_sensibles.reglas", allow_empty=False),
            faq=_faq(payload, "faq"),
            products=products,
            payments=payments,
            statements=statements,
            by_product=by_product,
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


def _products(raw: object) -> tuple[ProductKnowledge, ...]:
    if not isinstance(raw, list) or not raw or not all(isinstance(item, dict) for item in raw):
        raise KnowledgeError("productos debe ser una lista no vacia de tablas [[productos]]")
    products = []
    keys: set[str] = set()
    names: set[str] = set()
    for index, entry in enumerate(raw):
        where = f"productos[{index}]"
        _exact(where, entry, _PRODUCT_KEYS, _PRODUCT_OPTIONAL)
        key = _text(entry, "clave", f"{where}.clave")
        if _PRODUCT_REF.fullmatch(key) is None:
            raise KnowledgeError(f"{where}.clave debe ser un slug en minusculas (a-z, 0-9, guiones)")
        if key in keys:
            raise KnowledgeError(f"{where}.clave '{key}' ya la usa otro producto")
        keys.add(key)
        product = _product(entry, key=key, where=f"{where}.")
        # El nombre titula la seccion del producto: dos iguales no se distinguen.
        if product.offer_name in names:
            raise KnowledgeError(f"{where}.oferta.nombre ya lo usa otro producto")
        names.add(product.offer_name)
        products.append(product)
    return tuple(products)


def _product(table: Mapping[str, Any], *, key: str | None, where: str) -> ProductKnowledge:
    offer = _table(table, "oferta", f"{where}oferta")
    _exact(f"{where}oferta", offer, {"nombre", "descripcion", "precio", "moneda"}, {"garantia", "notas"})
    content = _table(table, "contenido", f"{where}contenido")
    _exact(f"{where}contenido", content, {"componentes"}, {"se_puede_decir"})
    unconfirmed: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    faq: tuple[FaqEntry, ...] = ()
    if key is not None:
        if "no_confirmado" in table:
            own = _table(table, "no_confirmado", f"{where}no_confirmado")
            _exact(f"{where}no_confirmado", own, {"items"})
            unconfirmed = _list(own, "items", f"{where}no_confirmado.items", allow_empty=False)
        if "promesas_prohibidas" in table:
            own = _table(table, "promesas_prohibidas", f"{where}promesas_prohibidas")
            _exact(f"{where}promesas_prohibidas", own, {"items"})
            forbidden = _list(own, "items", f"{where}promesas_prohibidas.items", allow_empty=False)
        faq = _faq(table, f"{where}faq")
    return ProductKnowledge(
        key=key,
        offer_name=_text(offer, "nombre", f"{where}oferta.nombre"),
        offer_description=_text(offer, "descripcion", f"{where}oferta.descripcion"),
        offer_price=_text(offer, "precio", f"{where}oferta.precio"),
        offer_currency=_text(offer, "moneda", f"{where}oferta.moneda"),
        offer_guarantee=_optional_text(offer, "garantia", f"{where}oferta.garantia"),
        offer_notes=_list(offer, "notas", f"{where}oferta.notas"),
        content_components=_list(content, "componentes", f"{where}contenido.componentes"),
        content_statements=_list(content, "se_puede_decir", f"{where}contenido.se_puede_decir"),
        unconfirmed=unconfirmed,
        forbidden_promises=forbidden,
        faq=faq,
    )


def _faq(table: Mapping[str, Any], where: str) -> tuple[FaqEntry, ...]:
    key = where.rsplit(".", 1)[-1]
    raw_entries = table.get(key, [])
    if not isinstance(raw_entries, list):
        raise KnowledgeError(f"{where} debe ser una lista de tablas [[{where}]]")
    faq = []
    for index, raw in enumerate(raw_entries):
        entry_where = f"{where}[{index}]"
        if not isinstance(raw, dict):
            raise KnowledgeError(f"{entry_where} debe ser una tabla")
        _exact(entry_where, raw, {"pregunta", "respuesta", "fuente", "vigencia"})
        faq.append(
            FaqEntry(
                question=_text(raw, "pregunta", f"{entry_where}.pregunta"),
                answer=_text(raw, "respuesta", f"{entry_where}.respuesta"),
                source=_text(raw, "fuente", f"{entry_where}.fuente"),
                valid_from=_text(raw, "vigencia", f"{entry_where}.vigencia"),
            )
        )
    return tuple(faq)


def _render_offer(product: ProductKnowledge) -> list[str]:
    lines = [
        "",
        "## Oferta",
        "",
        f"- Nombre: {product.offer_name}.",
        f"- Que es: {product.offer_description}",
        f"- Precio: {product.offer_currency} {product.offer_price}.",
        "- Garantia: "
        + (product.offer_guarantee if product.offer_guarantee else "no confirmada."),
    ]
    lines += _bullets(product.offer_notes)
    return lines


def _render_content(product: ProductKnowledge) -> list[str]:
    lines = ["", "## Contenido del programa", ""]
    if product.content_components:
        lines += ["Lista cerrada de componentes:", ""] + _bullets(product.content_components)
    else:
        lines.append(
            "No hay lista de componentes confirmada: si preguntan que incluye, "
            "cuantos modulos tiene o cuanto dura, deriva."
        )
    if product.content_statements:
        lines += ["", "Lo que si se puede decir:", ""] + _bullets(product.content_statements)
    return lines


def _render_faq(faq: tuple[FaqEntry, ...]) -> list[str]:
    if not faq:
        return []
    lines = ["", "## Preguntas frecuentes aprobadas", ""]
    for entry in faq:
        lines += [f"- P: {entry.question}", f"  R: {entry.answer}"]
    return lines


def _payments(raw: object) -> PaymentGuide:
    if not isinstance(raw, dict):
        raise KnowledgeError("pagos debe ser una tabla")
    _exact("pagos", raw, {"pais_principal", "medios"}, {"por_pais", "pasos", "rechazos", "reglas"})

    by_country = []
    for index, entry in enumerate(_tables(raw, "por_pais", "pagos.por_pais")):
        where = f"pagos.por_pais[{index}]"
        _exact(where, entry, {"pais"}, {"transferencia", "efectivo"})
        transfer = _optional_text(entry, "transferencia", f"{where}.transferencia")
        cash = _optional_text(entry, "efectivo", f"{where}.efectivo")
        if transfer is None and cash is None:
            raise KnowledgeError(f"{where} lleva transferencia, efectivo o los dos")
        by_country.append(
            CountryPaymentNames(country=_text(entry, "pais", f"{where}.pais"), transfer=transfer, cash=cash)
        )

    steps = []
    for index, entry in enumerate(_tables(raw, "pasos", "pagos.pasos")):
        where = f"pagos.pasos[{index}]"
        _exact(where, entry, {"medio", "pasos"})
        steps.append(
            PaymentSteps(
                method=_text(entry, "medio", f"{where}.medio"),
                steps=_list(entry, "pasos", f"{where}.pasos", allow_empty=False),
            )
        )

    refusals = []
    for index, entry in enumerate(_tables(raw, "rechazos", "pagos.rechazos")):
        where = f"pagos.rechazos[{index}]"
        _exact(where, entry, {"motivo", "que_hacer"}, {"como_llega"})
        refusals.append(
            RefusalGuide(
                reason=_text(entry, "motivo", f"{where}.motivo"),
                arrives_as=_list(entry, "como_llega", f"{where}.como_llega"),
                what_to_do=_text(entry, "que_hacer", f"{where}.que_hacer"),
            )
        )

    return PaymentGuide(
        main_country=_text(raw, "pais_principal", "pagos.pais_principal"),
        methods=_list(raw, "medios", "pagos.medios", allow_empty=False),
        by_country=tuple(by_country),
        steps=tuple(steps),
        refusals=tuple(refusals),
        rules=_list(raw, "reglas", "pagos.reglas"),
    )


def _render_payments(guide: PaymentGuide) -> list[str]:
    lines = [
        "",
        "## Pagos",
        "",
        f"Pais principal del negocio: {guide.main_country}.",
        "",
        "Medios de pago activos, los unicos que puedes ofrecer:",
        "",
    ]
    lines += _bullets(guide.methods)
    if guide.by_country:
        lines += [
            "",
            "Como se llaman la transferencia y el efectivo segun el pais de quien paga:",
            "",
        ]
        for entry in guide.by_country:
            names = []
            if entry.transfer:
                names.append(f"transferencia {entry.transfer}")
            if entry.cash:
                names.append(f"efectivo {entry.cash}")
            lines.append(f"- {entry.country}: {'; '.join(names)}.")
    for entry in guide.steps:
        lines += ["", f"Paso a paso para pagar con {entry.method}:", ""]
        lines += _numbered(entry.steps)
    if guide.refusals:
        lines += ["", "Pago rechazado, que hacer segun el motivo:", ""]
        for entry in guide.refusals:
            arrives = (
                " (llega como " + ", ".join(f"«{text}»" for text in entry.arrives_as) + ")"
                if entry.arrives_as
                else ""
            )
            lines.append(f"- {entry.reason}{arrives}: {entry.what_to_do}")
    if guide.rules:
        lines += ["", "Reglas de pagos:", ""] + _bullets(guide.rules)
    return lines


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " y " + names[-1]


def _tables(raw: Mapping[str, Any], key: str, where: str) -> list[Mapping[str, Any]]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise KnowledgeError(f"{where} debe ser una lista de tablas [[{where}]]")
    return value


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


def _table(payload: Mapping[str, Any], key: str, where: str | None = None) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise KnowledgeError(f"{where or key} debe ser una tabla")
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
