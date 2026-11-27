"""Las reglas de palabra clave: el texto de un link ``wa.me`` pone una etiqueta (Z1 3.9, ``keyword-rules-v1``).

Contrato: ``docs/contracts/keyword-rules-v1.md``. Mapa de la serie Z1, D4, D5 y D13.

Un link de WhatsApp por segmento (soporte, check-in, asistencia, alumnos) lleva
un texto prellenado. Una **regla** dice «si el primer mensaje trae esta palabra
clave, la conversacion lleva esta etiqueta». La redacta el Automation Expert y la
publica una persona de ``aprobadores_slack`` en la pagina de aprobacion; el
bridge la lee de la base (``get_live_keyword_rules_v1``) con cache corta.

En el turno del agente, con la historia canonica ya leida y antes de armar el
pedido (el mismo lugar y el mismo «primer mensaje» que ``campaign-inbound-entry-v1``):

1. Si el primer mensaje de la persona (desde el ultimo ``/nuevo``) es parte de
   este turno, la conversacion la abrio ella, y trae la palabra clave de una
   regla servida (el mismo matching normalizado que las campanas, palabras
   enteras en cualquier parte), la etiqueta se pone por el macro de la regla
   (``ProductLabeler``: verifica que el macro solo sume esa etiqueta, es
   idempotente y nunca lanza).
2. El turno suma una **nota fija** como ``system``: el primer mensaje es el texto
   de un link y no un pedido de hablar con una persona (D13: sin tocar el SOUL
   comun).

Precedencia (D-39.3): la campana gana. Una regla cuya palabra choca con la de una
campana cargada no se sirve. Solo se aplica una regla por conversacion: la de la
palabra que aparece primero en el mensaje (D-39.4).

La regla v1 no dispara flujos (D-39.1): ver el contrato, «El flujo». Desde Z2 3.9 un flujo nativo con
``dispara: {palabra_clave: <clave>}`` arranca por la regla (``keyword_flow_trigger``), detras de su flag.

Sin ``KEYWORD_RULES_ENABLED`` no se crea nada de esto y el turno es byte a byte el
de hoy. Los logs no llevan telefono, mail, nombre ni el texto de la persona: la
conversacion, la clave de la regla y la etiqueta.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .campaign_inbound_entry import (
    first_message_of_turn,
    keyword_in,
    normalize_entry_text,
    opened_by_the_person,
)

__all__ = [
    "CONTRACT",
    "NOTE_KIND",
    "KeywordRule",
    "KeywordRuleContext",
    "KeywordRuleInvalid",
    "KeywordRules",
    "KeywordTurn",
    "keyword_rules_setup",
    "keywords_clash",
    "parse_rule",
    "rule_blocks",
]

logger = logging.getLogger(__name__)

CONTRACT = "keyword-rules-v1"
FLAG = "KEYWORD_RULES_ENABLED"
REFRESH_SECONDS = 30.0
# En la procedencia del turno la nota va con este tipo: no es el bloque de un segmento.
NOTE_KIND = "nota_palabra_clave"
NOTE_KEY = "palabra-clave"
NOTE_VERSION = 1
NOTE_TEXT = (
    "# El primer mensaje es el texto de un link\n"
    "El primer mensaje de esta persona es el texto que trae escrito un link de WhatsApp "
    "(el botón de una página o de un mensaje nuestro): lo mandó tocando el link. No es un "
    "pedido de hablar con una persona del equipo ni una queja. No derives la conversación "
    "por ese mensaje: contesta como a cualquier primer mensaje y pregunta en qué puedes ayudar."
)
LABEL_SOURCE = "keyword_rule"
# Lo que ya usa el producto: no puede ser la etiqueta de una regla.
RESERVED_LABELS = frozenset({"cliente"})
RESERVED_LABEL_PREFIX = "automation_"
NORMALIZED_MIN = 3
NORMALIZED_MAX = 120

NOT_SERVED_DRAFT = "contenido_invalido"
NOT_SERVED_RULE = "choca_con_otra_regla"
NOT_SERVED_CAMPAIGN = "choca_con_una_campana"
NOT_SERVED_RESERVED = "etiqueta_o_macro_reservado"

# ``palabra_normalizada`` la calcula el bridge: si viene (una fila guardada), se recalcula igual.
_RULE_KEYS = frozenset({"clave", "nombre", "palabra_clave", "palabra_normalizada", "etiqueta", "macro_etiqueta"})


class KeywordRuleInvalid(ValueError):
    """Una regla que no sirve. El motivo lleva el campo y el problema, nunca el valor."""

    def __init__(self, code: str, reason: Mapping[str, Any]) -> None:
        super().__init__(code)
        self.code = code
        self.reason = dict(reason)


def _refused(field: str, problem: str, code: str = "keyword_rule_invalid") -> KeywordRuleInvalid:
    return KeywordRuleInvalid(code, {"field": field, "problem": problem})


def _line(raw: Mapping[str, Any], key: str, *, limit: int) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _refused(key, "required_text")
    text = " ".join(value.split())
    if len(text) > limit:
        raise _refused(key, f"longer_than_{limit}")
    return text


def parse_rule(raw: Any) -> dict[str, Any]:
    """La regla normalizada que se guarda: ``clave``, ``nombre``, ``palabra_clave``, ``palabra_normalizada``,
    ``etiqueta`` y ``macro_etiqueta``. Lanza ``KeywordRuleInvalid``."""

    if not isinstance(raw, Mapping):
        raise _refused("rule", "not_object")
    if "flujo" in raw:
        # D-39.1: la regla v1 solo etiqueta. El flujo queda para cuando el motor tenga el disparador.
        raise _refused("flujo", "not_supported_in_v1", code="keyword_rule_flow_not_supported")
    extra = set(raw) - _RULE_KEYS
    if extra:
        raise KeywordRuleInvalid("keyword_rule_invalid", {"field": "rule", "extra": sorted(extra)[:10]})
    key = raw.get("clave")
    if not isinstance(key, str) or not key or len(key) > 64 or not (
        key[0].isascii() and (key[0].islower() or key[0].isdigit())
    ) or any(not (c.isascii() and (c.islower() or c.isdigit() or c == "-")) for c in key):
        raise _refused("clave", "lowercase_digits_and_dashes")
    name = _line(raw, "nombre", limit=80)
    keyword = _line(raw, "palabra_clave", limit=200)
    normalized = normalize_entry_text(keyword)
    if not NORMALIZED_MIN <= len(normalized) <= NORMALIZED_MAX:
        raise _refused("palabra_clave", f"normalized_between_{NORMALIZED_MIN}_and_{NORMALIZED_MAX}")
    if normalized.replace(" ", "").isdigit():
        raise _refused("palabra_clave", "only_digits")
    label = raw.get("etiqueta")
    if not isinstance(label, str) or not label or len(label) > 40 or not (
        label[0].isascii() and (label[0].islower() or label[0].isdigit())
    ) or any(not (c.isascii() and (c.islower() or c.isdigit() or c in "-_")) for c in label):
        raise _refused("etiqueta", "chatwoot_label")
    macro = raw.get("macro_etiqueta")
    if type(macro) is not int or not 1 <= macro <= 999_999_999_999:
        raise _refused("macro_etiqueta", "positive_integer")
    return {
        "clave": key,
        "nombre": name,
        "palabra_clave": keyword,
        "palabra_normalizada": normalized,
        "etiqueta": label,
        "macro_etiqueta": macro,
    }


def keywords_clash(first: str, second: str) -> bool:
    """Si dos palabras clave (normalizadas) se pisan: iguales, o una contiene a la otra como palabras enteras.

    Con el matching de las campanas (contenida en cualquier parte), un mensaje con la
    mas larga trae tambien la mas corta: las dos se aplicarian al mismo primer mensaje.
    """

    return keyword_in(first, second) or keyword_in(second, first)


def _position(normalized_text: str, keyword: str) -> int:
    return f" {normalized_text} ".find(f" {keyword} ")


def wa_me_link(phone: object, keyword: str) -> str | None:
    """El link del boton: el numero de la bandeja con la palabra clave escrita."""

    digits = "".join(character for character in phone if character.isdigit()) if isinstance(phone, str) else ""
    return f"https://wa.me/{digits}?text={quote(keyword)}" if digits else None


@dataclass(frozen=True)
class KeywordRuleContext:
    """Contra que se mide una regla: lo reservado de la instancia y las palabras de las campanas cargadas."""

    reserved_macros: frozenset[int]
    reserved_labels: Callable[[], Iterable[str]]
    campaign_keywords: Callable[[], Iterable[str]]


def _finding(code: str, field: str) -> dict[str, Any]:
    return {"code": code, "field": field}


def rule_blocks(rule: Mapping[str, Any], *, context: KeywordRuleContext,
                others: Iterable[tuple[str, str]], rule_id: str | None = None) -> list[dict[str, Any]]:
    """Lo que frena una regla ya normalizada. ``others``: (rule_id, palabra normalizada) de las vivas."""

    blocks: list[dict[str, Any]] = []
    keyword = rule["palabra_normalizada"]
    if any(keywords_clash(keyword, normalize_entry_text(word)) for word in context.campaign_keywords()):
        blocks.append(_finding("keyword_rule_clashes_with_campaign", "palabra_clave"))
    if any(other_id != rule_id and keywords_clash(keyword, word) for other_id, word in others):
        blocks.append(_finding("keyword_rule_clashes_with_rule", "palabra_clave"))
    label = rule["etiqueta"]
    if label in RESERVED_LABELS or label.startswith(RESERVED_LABEL_PREFIX) or label in set(context.reserved_labels()):
        blocks.append(_finding("keyword_rule_label_reserved", "etiqueta"))
    if rule["macro_etiqueta"] in context.reserved_macros:
        blocks.append(_finding("keyword_rule_macro_reserved", "macro_etiqueta"))
    return blocks


def macro_block(actions: object, label: str) -> dict[str, Any] | None:
    """El macro tiene que sumar exactamente esta etiqueta (lo que despues exige ``ProductLabeler``)."""

    if actions != [{"action_name": "add_label", "action_params": [label]}]:
        return _finding("keyword_rule_macro_does_more_than_add_the_label", "macro_etiqueta")
    return None


@dataclass(frozen=True)
class KeywordRule:
    """Una regla servida."""

    rule_id: str
    key: str
    name: str
    keyword: str
    label: str
    macro_id: int


@dataclass(frozen=True)
class KeywordNote:
    """La nota del turno (D13), con la forma de un ``system`` extra de Hermes."""

    agent_replies_before: int
    kind: str = NOTE_KIND

    @property
    def key(self) -> str:
        return NOTE_KEY

    @property
    def version(self) -> int:
        return NOTE_VERSION

    @property
    def rendered(self) -> str:
        return NOTE_TEXT

    @property
    def sha256(self) -> str:
        return hashlib.sha256(NOTE_TEXT.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KeywordTurn:
    """Lo que la regla le suma a un turno: la regla que se aplico, la nota y el primer mensaje que la trajo.

    ``message_ref`` (Z2 3.9) es el id de Chatwoot de ese primer mensaje: el disparador del flujo
    (``keyword_flow_trigger``) lo usa para que la admision sea una por mensaje.
    """

    rule: KeywordRule | None = None
    note: KeywordNote | None = None
    message_ref: str | None = None


NO_KEYWORD = KeywordTurn()


class KeywordRules:
    """Las reglas vivas de la instancia y lo que hacen en el turno.

    ``fetch`` devuelve ``{"rules": [...]}`` de ``get_live_keyword_rules_v1``. Si falla, se
    sirve lo ultimo bueno (o nada, si nunca leyo). Una regla que no se puede servir no
    toca al resto.
    """

    def __init__(self, *, fetch: Callable[[], Awaitable[Mapping[str, Any]]], context: KeywordRuleContext,
                 make_labeler: Callable[[str, int], Any], reset_command: str, reset_confirmation: str,
                 refresh_seconds: float = REFRESH_SECONDS) -> None:
        self._fetch = fetch
        self.context = context
        self._make_labeler = make_labeler
        self._reset_command = reset_command
        self._reset_confirmation = reset_confirmation
        self._refresh_seconds = refresh_seconds
        self._rules: tuple[KeywordRule, ...] = ()
        self._not_served: dict[str, str] = {}
        self._labelers: dict[tuple[str, int], Any] = {}
        self._applied: tuple[tuple[str, str], ...] | None = None
        self._failures = 0
        self._task: asyncio.Task[None] | None = None

    # -- lo que se sirve ------------------------------------------------------------

    @property
    def rules(self) -> tuple[KeywordRule, ...]:
        return self._rules

    def live_keywords(self) -> tuple[tuple[str, str], ...]:
        """(rule_id, palabra normalizada) de las reglas servidas, para medir una regla nueva."""
        return tuple((rule.rule_id, rule.keyword) for rule in self._rules)

    def _campaign_clash(self, keyword: str) -> bool:
        return any(keywords_clash(keyword, normalize_entry_text(word)) for word in self.context.campaign_keywords())

    def serving_state(self, rule_id: str) -> str | None:
        """``served``, el motivo de por que no se sirve, o None si no es una publicada conocida."""
        rule = next((rule for rule in self._rules if rule.rule_id == rule_id), None)
        if rule is not None:
            return NOT_SERVED_CAMPAIGN if self._campaign_clash(rule.keyword) else "served"
        return self._not_served.get(rule_id)

    def status_line(self) -> str:
        """Para ``/ready``: cuantas se sirven, cuantas no y si la lectura viene andando."""
        state = "ok" if self._failures == 0 else f"failing:{self._failures}"
        return f"served={len(self._rules)},not_served={len(self._not_served)},refresh={state}"

    def match(self, text: str) -> KeywordRule | None:
        """La regla del mensaje: la de la palabra que aparece primero (a igual lugar, la mas larga). D-39.4."""
        normalized = normalize_entry_text(text)
        found = [
            (position, -len(rule.keyword), rule)
            for rule in self._rules
            if (position := _position(normalized, rule.keyword)) >= 0
        ]
        if not found:
            return None
        found.sort(key=lambda item: (item[0], item[1]))
        return found[0][2]

    def labeler(self, rule: KeywordRule) -> Any:
        # El mismo objeto entre recargas: recuerda el macro verificado y las conversaciones etiquetadas.
        return self._labelers.setdefault((rule.label, rule.macro_id), self._make_labeler(rule.label, rule.macro_id))

    # -- el turno -------------------------------------------------------------------

    async def turn(self, *, conversation_id: int, history: Sequence[Mapping[str, Any]], history_complete: bool,
                   messages: Sequence[Mapping[str, Any]], batch_refs: Iterable[str],
                   agent_replies_before: int) -> KeywordTurn:
        """Lo que la regla le suma a este turno. Nunca lanza."""
        try:
            return await self._turn(conversation_id=conversation_id, history=history,
                                    history_complete=history_complete, messages=messages,
                                    batch_refs=batch_refs, agent_replies_before=agent_replies_before)
        except Exception as exc:  # noqa: BLE001 - falla blanda: el turno sigue como hoy
            logger.warning("keyword_rule_failed conversation=%s error=%s", conversation_id, type(exc).__name__)
            return NO_KEYWORD

    async def _turn(self, *, conversation_id: int, history: Sequence[Mapping[str, Any]], history_complete: bool,
                    messages: Sequence[Mapping[str, Any]], batch_refs: Iterable[str],
                    agent_replies_before: int) -> KeywordTurn:
        if not self._rules or not history_complete:
            return NO_KEYWORD
        first = first_message_of_turn(messages, batch_refs)
        if first is None:
            return NO_KEYWORD
        # D-39.5: el texto de un link lo manda quien abre la conversacion. Un «soporte» como
        # respuesta a una plantilla nuestra no es el link.
        if not opened_by_the_person(history, reset_command=self._reset_command,
                                    reset_confirmation=self._reset_confirmation):
            return NO_KEYWORD
        rule = self.match(str(first.get("text") or ""))
        if rule is None:
            return NO_KEYWORD
        if self._campaign_clash(rule.keyword):
            # D-39.3: la campana gana (la entrada a la campana ya corrio en este turno).
            logger.info("keyword_rule_skipped conversation=%s rule=%s reason=%s", conversation_id, rule.key,
                        NOT_SERVED_CAMPAIGN)
            return NO_KEYWORD
        outcome = await self.labeler(rule).ensure(conversation_id, source=LABEL_SOURCE)
        logger.info("keyword_rule_label conversation=%s rule=%s label=%s outcome=%s", conversation_id, rule.key,
                    rule.label, outcome)
        return KeywordTurn(rule=rule, note=KeywordNote(agent_replies_before=agent_replies_before),
                           message_ref=str(first.get("_message_ref")) if first.get("_message_ref") is not None else None)

    # -- la recarga -----------------------------------------------------------------

    def _rebuild(self, rows: Sequence[Mapping[str, Any]]) -> None:
        rules: list[KeywordRule] = []
        not_served: dict[str, str] = {}
        for row in rows:
            rule_id = str(row.get("rule_id"))
            try:
                parsed = parse_rule(row.get("rule"))
            except KeywordRuleInvalid:
                not_served[rule_id] = NOT_SERVED_DRAFT
                continue
            stored = row.get("rule")
            if not isinstance(stored, Mapping) or stored.get("palabra_normalizada") != parsed["palabra_normalizada"]:
                not_served[rule_id] = NOT_SERVED_DRAFT
                continue
            label = parsed["etiqueta"]
            if (parsed["macro_etiqueta"] in self.context.reserved_macros or label in RESERVED_LABELS
                    or label.startswith(RESERVED_LABEL_PREFIX) or label in set(self.context.reserved_labels())):
                not_served[rule_id] = NOT_SERVED_RESERVED
                continue
            if any(keywords_clash(parsed["palabra_normalizada"], rule.keyword) for rule in rules):
                # Vienen por orden de aprobacion: la que se aprobo despues no se sirve.
                not_served[rule_id] = NOT_SERVED_RULE
                continue
            rules.append(KeywordRule(rule_id=rule_id, key=parsed["clave"], name=parsed["nombre"],
                                     keyword=parsed["palabra_normalizada"], label=label,
                                     macro_id=parsed["macro_etiqueta"]))
        self._rules = tuple(rules)
        self._not_served = not_served

    async def refresh(self) -> bool:
        """Lee la base y actualiza. Nunca lanza. False si no se pudo leer."""
        try:
            payload = await self._fetch()
            rows = payload["rules"]
            if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
                raise TypeError("rules")
        except Exception as error:  # noqa: BLE001 - la base caida no tira el turno de nadie
            self._failures += 1
            logger.warning("keyword_rules_refresh_failed failures=%s error=%s", self._failures, type(error).__name__)
            return False
        self._failures = 0
        key = tuple(sorted((str(row.get("rule_id")), str(row.get("fingerprint"))) for row in rows))
        if key == self._applied:
            return True
        self._rebuild(rows)
        self._applied = key
        for rule in self._rules:
            logger.info("keyword_rule_served rule=%s label=%s", rule.key, rule.label)
        for rule_id, reason in self._not_served.items():
            logger.warning("keyword_rule_not_served rule_id=%s reason=%s", rule_id, reason)
        return True

    async def start(self) -> None:
        await self.refresh()
        self._task = asyncio.create_task(self._loop(), name="keyword-rules-refresh")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_seconds)
            await self.refresh()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None


def keyword_rules_setup(settings: Any, control_client: Any, supabase: Any, *, campaign_entry: Any,
                        reset_command: str, reset_confirmation: str) -> KeywordRules | None:
    """Las reglas, o nada sin el flag. Falla cerrado.

    ``KEYWORD_RULES_ENABLED`` pide el manifiesto, el cliente de control de Chatwoot (el
    macro), la cuenta y el inbox, la base (las reglas vivas), las rutas del Automation
    Expert (``AUTOMATION_EXPERT_CHANGE_ENABLED``: las redacta) y la pagina
    (``FLOW_APPROVAL_ENABLED``: las aprueba una persona). ``campaign_entry`` es la entrada a
    las campanas (``campaign_inbound_entry_setup``), o None: sus palabras clave ganan.
    """

    from .product_label import ProductLabeler

    if not getattr(settings, "keyword_rules_enabled", False):
        return None
    manifest = getattr(settings, "instance_manifest", None)
    if manifest is None:
        raise ValueError(f"{FLAG} requires an instance manifest")
    if control_client is None:
        raise ValueError(f"{FLAG} requires the Chatwoot control client")
    if type(getattr(settings, "chatwoot_account_id", None)) is not int or type(
            getattr(settings, "chatwoot_inbox_id", None)) is not int:
        raise ValueError(f"{FLAG} requires CHATWOOT_ACCOUNT_ID and CHATWOOT_INBOX_ID")
    if supabase is None or not hasattr(supabase, "get_live_keyword_rules"):
        raise ValueError(f"{FLAG} requires the Supabase client")
    if not getattr(settings, "automation_expert_change_enabled", False):
        raise ValueError(f"{FLAG} requires AUTOMATION_EXPERT_CHANGE_ENABLED")
    if not getattr(settings, "flow_approval_enabled", False):
        raise ValueError(f"{FLAG} requires FLOW_APPROVAL_ENABLED")
    reserved_macros = frozenset(
        macro
        for macro in (
            getattr(settings, name, None)
            for name in ("chatwoot_pause_macro_id", "chatwoot_resume_macro_id", "chatwoot_opt_out_macro_id",
                         "chatwoot_client_label_macro_id")
        )
        if macro is not None
    ) | frozenset(
        product.chatwoot_label_macro_id
        for product in getattr(manifest, "products", ())
        if getattr(product, "chatwoot_label_macro_id", None) is not None
    )
    product_labels = frozenset(
        product.chatwoot_label for product in getattr(manifest, "products", ())
        if getattr(product, "chatwoot_label", None) is not None
    )
    declared = tuple(getattr(manifest, "agent_segments", ()))

    def reserved_labels() -> Iterable[str]:
        # Las de los productos y las de los segmentos (las campanas): una regla no las reusa.
        segments = getattr(campaign_entry, "segments", ()) if campaign_entry is not None else ()
        return (product_labels | {segment.label for segment in declared}
                | {segment.label for segment in segments})

    def campaign_keywords() -> Iterable[str]:
        if campaign_entry is not None:
            return [keyword for segment in campaign_entry.segments for keyword in segment.keywords]
        return [keyword for segment in declared for keyword in (getattr(segment, "entry_keywords", ()) or ())]

    refs = {"tenant_ref": manifest.tenant_ref, "ally_ref": manifest.ally_ref}

    async def fetch() -> Mapping[str, Any]:
        return await supabase.get_live_keyword_rules(refs)

    return KeywordRules(
        fetch=fetch,
        context=KeywordRuleContext(reserved_macros=reserved_macros, reserved_labels=reserved_labels,
                                   campaign_keywords=campaign_keywords),
        make_labeler=lambda label, macro_id: ProductLabeler(client=control_client, label=label, macro_id=macro_id),
        reset_command=reset_command,
        reset_confirmation=reset_confirmation,
    )
