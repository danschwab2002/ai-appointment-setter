"""La pagina de una solicitud de plantilla y el envio a Meta (template-request-v1 1.1.0, F-146 parte 3).

Rutas, solo con ``META_TEMPLATES_ENABLED`` (apagado, no existen y el cliente de
Meta ni se crea), con la misma sesion de «Entrar con Slack» y la misma lista de
aprobadores que la pagina de flujos:

* ``GET /flow-approval/t/{id}``: la plantilla como la veria la persona, en
  lenguaje de negocio y sin ids a la vista (la pagina sale en capturas): que se
  pide, que significa la categoria, los avisos y en que estado esta.
* ``POST /flow-approval/t/{id}/send``: «Mandar a Meta». Aprueba en la base, con
  el ID de Slack de la sesion, empieza el envio con el tope por dia (D8), mira
  en Meta si el nombre ya existe (un reintento no duplica) y si no la crea.
  Registra lo que Meta contesto, o el codigo del error. Un clic de mas no llama
  a Meta: la base dice que ya se mando o que hay un envio en curso.
* ``POST /flow-approval/t/{id}/discard``: «Descartar», solo si no llego a Meta.

El sondeo del estado, los avisos al canal y la sincronizacion del catalogo son
de la parte 4: hasta entonces, el estado se mira en esta pagina o en WhatsApp
Manager.

Lo que escribe la pagina y lo que manda a Meta sale de la fila validada: el
pedido congelado por la huella, nunca algo del formulario. El log lleva la ruta,
el id y el codigo; nunca el texto de la plantilla ni el token.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from .flow_approval import (
    _FINGERPRINT,
    DISCARD_REASON_MAX,
    PREFIX,
    PageTools,
    SessionInvalid,
    StoreRejected,
    StoreUnavailable,
    _canonical_uuid,
    _chip,
    _done_from_query,
    _esc,
    _hidden,
    _login,
    _page,
    _sha,
    _shell,
    _when,
)
from .meta_templates import MetaTemplate, MetaTemplatesClient, MetaTemplatesError, content_of

__all__ = ["describe_send_error", "render_request", "routes"]

logger = logging.getLogger(__name__)

DISCARD_DEFAULT_REASON = "descartada desde la pagina de aprobacion"
SEND_ATTEMPTS_MAX = 5
# Lo que la base acepta como estado de la creacion (20261102000200).
_RECORDABLE_STATUS = {"PENDING", "IN_APPEAL", "APPROVED", "REJECTED"}
_RECORDABLE_CATEGORY = {"UTILITY", "MARKETING", "AUTHENTICATION"}
_SENDABLE = {"validada", "aprobada", "enviada"}
_NAMED = re.compile(r"\{\{\s*([^{}\s]*)\s*\}\}")

_CATEGORY_WORD = {"UTILITY": "utilidad", "MARKETING": "marketing", "AUTHENTICATION": "autenticación"}
_CATEGORY_MEANING = {
    "UTILITY": ("Utilidad: sobre algo que la persona ya hizo (su compra, su pago, su turno). Si Meta entiende que "
                "vende, la aprueba como marketing y un flujo que la pide de utilidad no la puede usar."),
    "MARKETING": ("Marketing: invita a comprar. Lleva el botón «No más mensajes», cuesta más por mensaje y Meta "
                  "puede no entregarla a quien ya recibió muchas de marketing."),
}
_WARNING = {
    "template_utility_sounds_promotional": "Va como utilidad y habla de descuento u oferta: Meta la puede aprobar "
                                           "como marketing.",
    "template_few_words_for_variables": "Tiene pocas palabras para tantos datos: Meta la puede rechazar.",
    "template_url_and_quick_replies": "Un link junto con respuestas rápidas no se ve en WhatsApp de escritorio.",
    "template_header_formatting": "Meta puede rechazar formato o emojis en el encabezado.",
}

# El estado, en palabras: etiqueta, color y que quiere decir.
_STATE = {
    "borrador": ("Borrador", "chip--info", "El agente todavía la está armando: no se puede mandar hasta que quede "
                                           "validada."),
    "validada": ("Esperando el sí", "chip--info", "Lista para mandar a Meta."),
    "aprobada": ("Aprobada, sin mandar", "chip--warn", "Se aprobó pero no se llegó a mandar a Meta."),
    "enviada": ("Mandándose", "chip--warn", "Se empezó a mandar y Meta todavía no confirmó."),
    "en_revision": ("En revisión en Meta", "chip--info", "Meta la está revisando. Suele tardar minutos y puede llegar "
                                                         "a 24 horas. Nadie recibe nada todavía."),
    "sin_respuesta_de_meta": ("Sin respuesta de Meta", "chip--warn", "Pasaron 48 horas y Meta no contestó. Sigue "
                                                                     "esperando."),
    "aprobada_meta": ("Aprobada por Meta", "chip--ok", "Meta la aprobó. Falta que llegue al catálogo de la bandeja "
                                                       "para que el flujo la pueda usar."),
    "rechazada": ("Rechazada por Meta", "chip--warn", "Meta la rechazó. Se corrige con una solicitud nueva, con el "
                                                      "número siguiente."),
    "en_catalogo": ("En el catálogo", "chip--ok", "Ya se puede usar en un flujo."),
    "vencida": ("Vencida", "chip--warn", "Pasaron las 24 horas sin mandarla. Pedile al agente que la valide de "
                                         "nuevo."),
    "descartada": ("Descartada", None, "No se mandó a Meta."),
}
_REJECTED_REASON = {
    "INCORRECT_CATEGORY": "La categoría no corresponde al contenido.",
    "PROMOTIONAL": "Vende, y estaba pedida como utilidad.",
    "INVALID_FORMAT": "La forma no cumple las reglas de Meta.",
    "TAG_CONTENT_MISMATCH": "El contenido no corresponde a la categoría pedida.",
    "ABUSIVE_CONTENT": "Meta la considera contenido abusivo.",
    "SCAM": "Meta la considera engañosa.",
}

_DONE_OK = {"enviada": "Listo: se mandó a Meta. Ahora la está revisando.",
            "meta_aprobada": "Listo: Meta la aprobó en el momento.",
            "ya_enviada": "Esta plantilla ya se había mandado a Meta: no se mandó de nuevo.",
            "descartada": "Descartada. No se mandó a Meta."}
_DONE_WARN = {
    "meta_rechazada": "Meta la rechazó en el momento.",
    "meta_sin_registrar": ("Meta contestó pero no se pudo anotar la respuesta. Volvé a tocar «Mandar a Meta» en dos "
                           "minutos: se busca la plantilla y no se duplica."),
    "meta_template_daily_cap": "Se llegó al tope de plantillas por día de esta instancia. Probá mañana.",
    "meta_template_request_expired": "Venció antes de mandarla. Pedile al agente que la valide de nuevo.",
    "meta_template_send_in_progress": ("Hay un envío en curso. Esperá dos minutos antes de volver a intentar: no se "
                                       "duplica."),
    "meta_template_send_attempts_exhausted": ("Se intentó cinco veces sin que Meta la tomara. Pedile al agente una "
                                              "versión nueva, con el número siguiente."),
    "meta_template_not_validated": "Todavía no está validada: no se puede mandar.",
    "meta_template_not_approved": "No está aprobada para mandar.",
    "meta_template_not_sending": "La solicitud ya no se estaba mandando. Recargá la página para ver cómo quedó.",
    "meta_template_request_final": "Ya no se puede descartar: ya llegó a Meta o se cerró.",
    "meta_template_fingerprint_mismatch": "La solicitud cambió desde que la abriste. Volvé a abrirla y mirála de nuevo.",
    "meta_template_fingerprint_invalid": "La solicitud cambió desde que la abriste. Volvé a abrirla y mirála de nuevo.",
    "flow_store_text_invalid": "El motivo no se pudo guardar: escribilo en una línea.",
}
_SEND_ERROR = {
    "meta_unreachable": ("Meta no contestó. Puede que la haya recibido: al volver a mandar primero se busca, así que "
                         "no se duplica."),
    "meta_unexpected_answer": ("Meta contestó algo que no se reconoce. Volvé a intentar en un rato: primero se busca "
                               "la plantilla, así que no se duplica."),
    "meta_list_too_long": "La cuenta de WhatsApp tiene demasiadas plantillas para revisar si esta ya existe.",
    "meta_request_missing": "A esta solicitud le falta el pedido armado: pedile al agente que la valide de nuevo.",
    "meta_name_taken_other_text": ("Ya existe en Meta una plantilla con este nombre y otro contenido. Pedile al "
                                   "agente una versión nueva, con el número siguiente."),
    "meta_name_taken_unusable": ("Ya existe en Meta una plantilla con este nombre, pero está pausada o desactivada. "
                                 "Pedile al agente una versión nueva, con el número siguiente."),
}
_META_CODE = {
    "190": "La clave de Meta de esta instancia venció o la revocaron. Hay que generar una nueva.",
    "3": "La clave de Meta de esta instancia no tiene permiso para crear plantillas.",
    "10": "La clave de Meta de esta instancia no tiene permiso para crear plantillas.",
    "200": "La clave de Meta de esta instancia no tiene permiso para crear plantillas.",
    "4": "Meta pidió esperar: se llegó al límite de pedidos. Probá más tarde.",
    "80007": "Meta pidió esperar: la cuenta llegó a su límite de pedidos. Probá más tarde.",
    "130429": "Meta pidió esperar: se llegó al límite de pedidos. Probá más tarde.",
    "100": "Meta no aceptó el pedido tal como salió. Pedile al agente que revise la plantilla.",
    "131009": "Meta no aceptó uno de los datos del pedido. Pedile al agente que revise la plantilla.",
    "2388019": "La cuenta de WhatsApp llegó al tope de plantillas que permite Meta.",
    "2388040": "Un texto de la plantilla supera el largo que permite Meta.",
    "2388047": "Meta no aceptó el formato del encabezado.",
    "2388072": "Meta no aceptó el formato del cuerpo del mensaje.",
    "2388073": "Meta no aceptó el formato del pie del mensaje.",
    "2388293": "La plantilla tiene demasiados datos para lo corta que es.",
    "2388299": "La plantilla no puede empezar ni terminar con un dato.",
}
_SEND_ERROR_FALLBACK = "Meta no aceptó el pedido. Quedó registrado; si se repite, hay que revisarlo con el técnico."


def describe_send_error(code: object) -> str:
    """Un codigo de error del envio, en lenguaje de negocio. Nunca se muestra el codigo crudo."""
    if not isinstance(code, str):
        return _SEND_ERROR_FALLBACK
    if code in _SEND_ERROR:
        return _SEND_ERROR[code]
    parts = code.split(":")
    if parts[0] == "meta":
        for part in reversed(parts[1:]):
            if part in _META_CODE:
                return _META_CODE[part]
    return _SEND_ERROR_FALLBACK


def _render_done(done: Mapping[str, str]) -> str:
    code = done.get("hecho")
    if not code:
        return ""
    if code in _DONE_OK:
        return f'<div class="notice notice--ok" role="status"><p>{_esc(_DONE_OK[code])}</p></div>'
    text = _DONE_WARN.get(code) or describe_send_error(code)
    return f'<div class="notice notice--warn" role="status"><p>{_esc(text)}</p></div>'


def _with_marks(text: object) -> str:
    return _NAMED.sub(lambda match: f"[{match.group(1)}]", text) if isinstance(text, str) else ""


def _preview(found: Mapping[str, Any]) -> Mapping[str, Any]:
    """La burbuja guardada al validar; en un borrador, la que sale de lo que el agente escribio."""
    validation = found.get("validation")
    preview = validation.get("preview") if isinstance(validation, Mapping) else None
    bubble = preview.get("burbuja") if isinstance(preview, Mapping) else None
    if isinstance(bubble, Mapping):
        return bubble
    draft = found.get("draft") if isinstance(found.get("draft"), Mapping) else {}
    return {"encabezado": draft.get("encabezado"), "cuerpo": _with_marks(draft.get("cuerpo")),
            "pie": draft.get("pie"), "botones": draft.get("botones") or []}


def _bubble(preview: Mapping[str, Any]) -> str:
    inner = ""
    if preview.get("encabezado"):
        inner += f'<div class="bubble-head">{_esc(preview.get("encabezado"))}</div>'
    inner += _esc(preview.get("cuerpo") or "")
    if preview.get("pie"):
        inner += f'<div class="bubble-foot">{_esc(preview.get("pie"))}</div>'
    buttons = preview.get("botones")
    for button in buttons if isinstance(buttons, list) else []:
        if isinstance(button, Mapping):
            inner += f'<span class="bubble-button">{_esc(button.get("texto"))}</span>'
    return f'<div class="message"><div class="bubble">{inner}</div></div>'


def _button_lines(preview: Mapping[str, Any]) -> str:
    buttons = preview.get("botones")
    lines = ""
    for button in buttons if isinstance(buttons, list) else []:
        if not isinstance(button, Mapping):
            continue
        kind, label = button.get("tipo"), _esc(button.get("texto"))
        if kind == "checkout":
            lines += f"<li>«{label}» lleva al link de pago de cada persona.</li>"
        elif kind == "ficha":
            lines += f"<li>«{label}» lleva a la ficha de pago de cada persona.</li>"
        elif kind == "url_fija":
            lines += f"<li>«{label}» lleva a {_esc(button.get('url') or button.get('link'))}.</li>"
        elif button.get("texto") == "No más mensajes":
            lines += f"<li>«{label}» da de baja a la persona: no le llegan más mensajes.</li>"
        else:
            lines += f"<li>«{label}» es una respuesta rápida.</li>"
    return f'<ul class="meta">{lines}</ul>' if lines else ""


def _warnings(found: Mapping[str, Any]) -> str:
    validation = found.get("validation")
    result = validation.get("result") if isinstance(validation, Mapping) else None
    warnings = result.get("warnings") if isinstance(result, Mapping) else None
    lines = ""
    for warning in warnings if isinstance(warnings, list) else []:
        if isinstance(warning, Mapping):
            lines += f"<li>{_esc(_WARNING.get(str(warning.get('code')), warning.get('reason')))}</li>"
    return f'<div class="notice notice--warn"><p>Para mirar antes de mandar:</p><ul>{lines}</ul></div>' if lines else ""


def _meta_state_lines(found: Mapping[str, Any], *, me: str | None, time_zone: str | None) -> str:
    parts: list[str] = []
    if found.get("approved_at"):
        who = "vos" if found.get("approved_by_ref") == me else "una persona de la lista"
        parts.append(f"La aprobó {who} el {_when(found.get('approved_at'), time_zone)}.")
    if found.get("sent_at"):
        parts.append(f"Se mandó a Meta el {_when(found.get('sent_at'), time_zone)}.")
    if found.get("recategorized") is True:
        final = _CATEGORY_WORD.get(str(found.get("meta_category")), found.get("meta_category"))
        parts.append(f"Meta la aprobó como {_esc(final)}, otra categoría que la pedida.")
    if found.get("status") == "rechazada":
        reason = _REJECTED_REASON.get(str(found.get("meta_rejected_reason")))
        if reason:
            parts.append(_esc(reason))
    if found.get("status") == "enviada" and isinstance(found.get("last_send_error"), str):
        parts.append(_esc(describe_send_error(found.get("last_send_error"))))
    return "".join(f"<p>{part}</p>" for part in parts)


def _render_actions(found: Mapping[str, Any], request_id: str, *, csrf: str) -> str:
    status = found.get("effective_status")
    path = f"{PREFIX}/t/{_esc(request_id)}"
    can_send = status in _SENDABLE and isinstance(found.get("fingerprint"), str)
    exhausted = status == "enviada" and isinstance(found.get("send_attempts"), int) \
        and found["send_attempts"] >= SEND_ATTEMPTS_MAX
    parts: list[str] = []
    if can_send and not exhausted:
        retry = status in ("aprobada", "enviada")
        label = "Volver a mandar" if retry else "Mandar a Meta"
        parts.append(f'<form method="post" action="{path}/send">{_hidden("csrf_token", csrf)}'
                     f'{_hidden("fingerprint", found.get("fingerprint"))}'
                     f'<button type="submit" class="button primary">{label}</button></form>'
                     '<p class="meta">Se manda a Meta. Nadie recibe nada todavía. El flujo que la usa se aprueba '
                     "aparte, cuando Meta la apruebe.</p>")
    if status in ("validada", "aprobada"):
        parts.append(f'<div class="divider"></div><form method="post" action="{path}/discard">'
                     f'{_hidden("csrf_token", csrf)}'
                     f'<input type="text" name="motivo" maxlength="{DISCARD_REASON_MAX}" '
                     'placeholder="Motivo (opcional)" autocomplete="off">'
                     '<button type="submit" class="button">Descartar</button></form>')
    return "".join(parts)


def _flow_label(proposal: Mapping[str, Any] | None, pointer: object, product_name: str) -> str | None:
    if not isinstance(proposal, Mapping) or not isinstance(pointer, str):
        return None
    match = re.match(r"^/items/([0-9]+)(/|$)", pointer)
    items = proposal.get("items")
    if match is None or not isinstance(items, list) or int(match.group(1)) >= len(items):
        return None
    item = items[int(match.group(1))]
    flow_key = item.get("flujo") if isinstance(item, Mapping) else None
    if not isinstance(flow_key, str) or not flow_key:
        return None
    name = flow_key.replace("_", " ").capitalize()
    return f"{name} de {product_name}" if product_name else name


def render_request(found: Mapping[str, Any], request_id: str, *, proposal: Mapping[str, Any] | None = None,
                   csrf: str = "", done: Mapping[str, str] | None = None, me: str | None = None,
                   time_zone: str | None = None, product_name: str = "") -> str:
    """El cuerpo de la pagina. Sin el id, la huella ni el nombre tecnico a la vista."""
    status = str(found.get("effective_status") or found.get("status"))
    label, chip, meaning = _STATE.get(status, ("—", None, ""))
    category = found.get("category_requested")
    word = _CATEGORY_WORD.get(str(category), "")
    flow = _flow_label(proposal, found.get("step_pointer"), product_name)
    what = f"Pedido a Meta: una plantilla nueva de <strong>{_esc(word)}</strong>"
    what += f" para el flujo <strong>{_esc(flow)}</strong>." if flow else "."
    preview = _preview(found)
    done_html = _render_done(done or {})
    purpose = found.get("purpose")
    purpose_html = (f'<p class="meta">Para qué, según el agente: {_esc(purpose)}</p>'
                    if isinstance(purpose, str) and purpose else "")
    actions = _render_actions(found, request_id, csrf=csrf)
    side = (f'<section class="panel panel--side"><div class="panel-heading"><h2>{_esc(label)}</h2>'
            f"<p>{_esc(meaning)}</p></div>"
            f'<div class="panel-body">{_meta_state_lines(found, me=me, time_zone=time_zone)}{actions}</div></section>')
    template_name = found.get("template_name")
    finder = ""
    if found.get("sent_at") and isinstance(template_name, str):
        finder = ('<details class="meta"><summary>Cómo buscarla en WhatsApp Manager</summary>'
                  f"<p>Buscá por el nombre <code>{_esc(template_name)}</code>, idioma {_esc(found.get('language'))}."
                  "</p></details>")
    return (f'<header class="page-header"><p class="meta"><a href="{PREFIX}/">Aprobación de flujos</a></p>'
            f'<h1>Plantilla nueva para WhatsApp</h1><div class="meta">{_chip(label, chip)}</div></header>'
            f"{done_html}"
            '<div class="layout"><div class="stack">'
            f'<section class="panel"><div class="panel-heading"><p>{what}</p></div>'
            f'<div class="panel-body">{_bubble(preview)}{_button_lines(preview)}{purpose_html}'
            f"{finder}</div></section>"
            f'<section class="panel"><div class="panel-body"><p>{_esc(_CATEGORY_MEANING.get(str(category), ""))}</p>'
            f"{_warnings(found)}</div></section>"
            f"</div>{side}</div>")


def routes(*, meta: MetaTemplatesClient, daily_cap: int, product_name: str) -> Callable[[FastAPI, PageTools], None]:
    """Las rutas, para ``register_flow_approval_routes(extra_routes=...)``."""

    def register(app: FastAPI, tools: PageTools) -> None:
        store, instance = tools.store, tools.instance

        def login(request_id: str) -> Response:
            return tools.logged_out(_login(None, template=request_id))

        @app.get(PREFIX + "/t/{request_id}")
        async def template_request_page(request_id: str, request: Request) -> Response:
            if not _canonical_uuid(request_id):
                return _page("Plantilla", "No existe esa plantilla.", 404)
            who = await tools.who(request)
            if who is None:
                return login(request_id)
            user, secret = who
            try:
                found = await store.get_template_request(instance, session_hash=_sha(secret), request_id=request_id)
            except SessionInvalid:
                return login(request_id)
            except StoreRejected:
                return _page("Plantilla", "No existe esa plantilla en esta instancia.", 404)
            except StoreUnavailable:
                logger.info("flow_approval route=template status=503")
                return _page("Plantilla", "El almacén no contestó. Probá en un rato.", 503)
            proposal = None
            if isinstance(found.get("proposal_id"), str):
                # El nombre del flujo es un dato de mas: si no se puede leer, la pagina no se cae.
                try:
                    proposal = await store.get_proposal(instance, session_hash=_sha(secret),
                                                        proposal_id=found["proposal_id"])
                except (StoreRejected, StoreUnavailable):
                    proposal = None
                except SessionInvalid:
                    return login(request_id)
            logger.info("flow_approval route=template request=%s status=%s", request_id, found.get("status"))
            csrf = tools.config.csrf_token(secret)
            body = render_request(found, request_id, proposal=proposal, csrf=csrf,
                                  done=_done_from_query(request, tools.config, secret), me=user,
                                  time_zone=tools.time_zone, product_name=product_name)
            return HTMLResponse(_shell("Plantilla", body, user=user, csrf=csrf, show_user_id=False))

        async def _deliver(begun: Mapping[str, Any]) -> tuple[MetaTemplate | None, str | None]:
            """Lo que Meta tiene al final: la plantilla, o el codigo de por que no."""
            request_body = begun.get("meta_request")
            name, language = begun.get("template_name"), begun.get("language")
            if not isinstance(request_body, Mapping) or not isinstance(name, str) or not isinstance(language, str):
                return None, "meta_request_missing"
            try:
                existing = await meta.find(name, language)
                if existing is None:
                    template = await meta.create(request_body)
                elif existing.content is None or existing.content != content_of(request_body.get("components")):
                    # Mismo nombre e idioma con otro encabezado, cuerpo, pie o botones: no es un reintento
                    # nuestro. Adoptarla podria dejar en un flujo una plantilla sin la baja.
                    return None, "meta_name_taken_other_text"
                elif existing.status not in _RECORDABLE_STATUS:
                    return None, "meta_name_taken_unusable"
                else:
                    template = existing
            except MetaTemplatesError as error:
                return None, error.code
            if template.status not in _RECORDABLE_STATUS or template.category not in _RECORDABLE_CATEGORY:
                return None, "meta_unexpected_answer"
            return template, None

        @app.post(PREFIX + "/t/{request_id}/send")
        async def template_request_send(request_id: str, request: Request) -> Response:
            if not _canonical_uuid(request_id):
                return _page("Plantilla", "No existe esa plantilla.", 404)
            checked = await tools.write_form(request, title="Mandar a Meta", fields={"csrf_token", "fingerprint"},
                                             template=request_id)
            if isinstance(checked, Response):
                return checked
            _, secret, form = checked
            page = f"{PREFIX}/t/{request_id}"
            if not _FINGERPRINT.fullmatch(form["fingerprint"]):
                return _page("Mandar a Meta", "Formulario inválido.", 422)
            session = _sha(secret)
            try:
                approved = await store.approve_template_request(instance, session_hash=session, request_id=request_id,
                                                                fingerprint=form["fingerprint"])
                if approved.get("ok") is not True:
                    return tools.back(secret, page, hecho=str(approved.get("reason_code") or "meta_template_not_approved"))
                if approved.get("status") not in ("aprobada", "enviada"):
                    # Ya salio: un clic de mas no vuelve a Meta.
                    logger.info("flow_approval route=template_send request=%s result=already", request_id)
                    return tools.back(secret, page, hecho="ya_enviada")
                begun = await store.begin_template_send(instance, session_hash=session, request_id=request_id,
                                                        fingerprint=form["fingerprint"], daily_cap=daily_cap)
                if begun.get("ok") is not True:
                    code = str(begun.get("reason_code") or "meta_template_not_approved")
                    logger.info("flow_approval route=template_send request=%s result=%s", request_id, code)
                    return tools.back(secret, page, hecho=code)
                template, error_code = await _deliver(begun)
                try:
                    recorded = await store.record_template_sent(
                        instance, session_hash=session, request_id=request_id,
                        meta_template_id=template.template_id if template else None,
                        meta_status=template.status if template else None,
                        meta_category=template.category if template else None, error_code=error_code)
                except (StoreUnavailable, StoreRejected):
                    logger.warning("flow_approval route=template_send request=%s result=not_recorded", request_id)
                    return tools.back(secret, page, hecho="meta_sin_registrar")
            except SessionInvalid:
                return login(request_id)
            except StoreRejected as rejected:
                if rejected.code == "meta_template_request_not_found":
                    return _page("Plantilla", "No existe esa plantilla en esta instancia.", 404)
                return tools.back(secret, page, hecho=rejected.code)
            except StoreUnavailable:
                return _page("Mandar a Meta", "El almacén no contestó. Probá en un rato.", 503)
            if error_code is not None:
                logger.info("flow_approval route=template_send request=%s result=error code=%s", request_id, error_code)
                return tools.back(secret, page, hecho=error_code)
            if recorded.get("ok") is not True:
                code = str(recorded.get("reason_code") or "meta_template_not_sending")
                logger.warning("flow_approval route=template_send request=%s result=%s", request_id, code)
                return tools.back(secret, page, hecho="ya_enviada" if code == "meta_template_already_sent" else code)
            status = recorded.get("status")
            result = {"en_revision": "enviada", "aprobada_meta": "meta_aprobada", "rechazada": "meta_rechazada"}.get(
                str(status), "enviada")
            logger.info("flow_approval route=template_send request=%s result=%s", request_id, result)
            return tools.back(secret, page, hecho=result)

        @app.post(PREFIX + "/t/{request_id}/discard")
        async def template_request_discard(request_id: str, request: Request) -> Response:
            if not _canonical_uuid(request_id):
                return _page("Plantilla", "No existe esa plantilla.", 404)
            checked = await tools.write_form(request, title="Descartar", fields={"csrf_token"},
                                             optional=frozenset({"motivo"}), template=request_id)
            if isinstance(checked, Response):
                return checked
            _, secret, form = checked
            page = f"{PREFIX}/t/{request_id}"
            reason = form.get("motivo", "").strip() or DISCARD_DEFAULT_REASON
            if len(reason) > DISCARD_REASON_MAX or any(not character.isprintable() for character in reason):
                return tools.back(secret, page, hecho="flow_store_text_invalid")
            try:
                discarded = await store.discard_template_request(instance, session_hash=_sha(secret),
                                                                 request_id=request_id, reason=reason)
            except SessionInvalid:
                return login(request_id)
            except StoreRejected as rejected:
                if rejected.code == "meta_template_request_not_found":
                    return _page("Plantilla", "No existe esa plantilla en esta instancia.", 404)
                return tools.back(secret, page, hecho=rejected.code)
            except StoreUnavailable:
                return _page("Descartar", "El almacén no contestó. Probá en un rato.", 503)
            code = "descartada" if discarded.get("ok") is True else str(
                discarded.get("reason_code") or "meta_template_request_final")
            logger.info("flow_approval route=template_discard request=%s result=%s", request_id, code)
            return tools.back(secret, page, hecho=code)

    return register
