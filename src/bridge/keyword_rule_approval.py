"""La pagina de una regla de palabra clave (``keyword-rules-v1``, Z1 3.9, D4 del mapa).

Rutas, solo con ``KEYWORD_RULES_ENABLED`` (apagado no existen), con la misma sesion
de «Entrar con Slack» y la misma lista de aprobadores que la pagina de flujos:

* ``GET  /flow-approval/w/{id}``: la regla en lenguaje de negocio: el texto del link,
  la etiqueta que pone, el link ``wa.me`` y si se esta usando.
* ``POST .../publish``: «Aprobar y publicar». **El si de una persona de la lista.**
  Antes de llamar a la base, el bridge vuelve a mirar que la palabra no choque con
  otra regla viva ni con una campana y que el macro solo sume la etiqueta.
* ``POST .../pause``, ``POST .../resume``: «Pausar» y «Reanudar» (reanudar vuelve a mirar los choques).
* ``POST .../discard``: «Descartar», un borrador o una pausada.

Lo que se publica es la fila guardada, atada por la huella: nunca algo del
formulario. El log lleva la ruta, el id y el codigo; nunca el texto de la regla.
Quien perdio la sesion entra y vuelve al inicio de la pagina (no a la regla): el
link de la regla se abre de nuevo.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from . import keyword_rules as rules
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
)
from .keyword_rule_api import instance_blocks

__all__ = ["render_rule", "routes"]

logger = logging.getLogger(__name__)

DISCARD_DEFAULT_REASON = "descartada desde la pagina de aprobacion"
PAUSE_DEFAULT_REASON = "pausada desde la pagina de aprobacion"

_STATE = {
    "borrador": ("Esperando el sí", "chip--info", "Lista para revisar. No pone ninguna etiqueta hasta que la apruebes."),
    "publicada": ("Andando", "chip--ok", "Quien empieza la conversación con este texto recibe la etiqueta."),
    "pausada": ("Pausada", "chip--warn", "No pone la etiqueta. Podés reanudarla o descartarla."),
    "descartada": ("Descartada", None, "No pone ninguna etiqueta."),
}
_DONE_OK = {"publicada": "Listo: la regla quedó andando.", "pausada": "Listo: la regla quedó pausada.",
            "reanudada": "Listo: la regla volvió a estar andando.", "descartada": "Descartada."}
_DONE_WARN = {
    "keyword_rule_clashes_with_campaign": "La palabra choca con la de una campaña: la campaña gana. Cambiá el texto.",
    "keyword_rule_clashes_with_rule": "La palabra choca con la de otra regla andando. Cambiá el texto.",
    "keyword_rule_label_reserved": "Esa etiqueta ya la usa el producto o una campaña.",
    "keyword_rule_macro_reserved": "Ese macro ya lo usa el producto.",
    "keyword_rule_macro_does_more_than_add_the_label": "El macro hace algo más que sumar la etiqueta.",
    "keyword_rule_macro_unreadable": "No se pudo leer el macro en Chatwoot. Probá en un rato.",
    "keyword_rule_word_taken": "Esa palabra ya está en otra regla (andando o pausada).",
    "keyword_rule_live_limit": "Ya hay 20 reglas andando: pausá o descartá una antes.",
    "keyword_rule_not_live": "La regla no estaba andando.",
    "keyword_rule_not_paused": "La regla no estaba pausada.",
    "keyword_rule_final": "Ya no se puede: la regla se cerró.",
    "keyword_rule_live_pause_first": "Para descartarla, primero pausala.",
    "keyword_rule_fingerprint_mismatch": "La regla cambió desde que la abriste. Volvé a abrirla y mirala de nuevo.",
    "keyword_rule_fingerprint_invalid": "La regla cambió desde que la abriste. Volvé a abrirla y mirala de nuevo.",
    "flow_store_text_invalid": "El motivo no se pudo guardar: escribilo en una línea.",
}
_NOT_SERVED = {
    rules.NOT_SERVED_CAMPAIGN: "Está aprobada, pero NO se está usando: la palabra choca con la de una campaña, y gana la campaña.",
    rules.NOT_SERVED_RULE: "Está aprobada, pero NO se está usando: la palabra choca con la de otra regla aprobada antes.",
    rules.NOT_SERVED_DRAFT: "Está aprobada, pero NO se está usando: el contenido no se pudo leer.",
    rules.NOT_SERVED_RESERVED: "Está aprobada, pero NO se está usando: la etiqueta o el macro los usa el producto.",
}


class _Calls:
    """Las cinco funciones de la pagina (20261126400000), con el cliente y la clave de la pagina."""

    def __init__(self, store: Any) -> None:
        self._rpc: Callable[[str, Mapping[str, Any]], Awaitable[dict[str, Any]]] = store._rpc

    async def get(self, instance: Mapping[str, str], session: str, rule_id: str) -> dict[str, Any]:
        return await self._rpc("approver_get_keyword_rule_v1", {
            "p_instance": instance, "p_session_hash": session, "p_rule_id": rule_id})

    async def publish(self, instance: Mapping[str, str], session: str, rule_id: str,
                      fingerprint: str) -> dict[str, Any]:
        return await self._rpc("approver_publish_keyword_rule_v1", {
            "p_instance": instance, "p_session_hash": session, "p_rule_id": rule_id, "p_fingerprint": fingerprint})

    async def pause(self, instance: Mapping[str, str], session: str, rule_id: str, reason: str) -> dict[str, Any]:
        return await self._rpc("approver_pause_keyword_rule_v1", {
            "p_instance": instance, "p_session_hash": session, "p_rule_id": rule_id, "p_reason": reason})

    async def resume(self, instance: Mapping[str, str], session: str, rule_id: str) -> dict[str, Any]:
        return await self._rpc("approver_resume_keyword_rule_v1", {
            "p_instance": instance, "p_session_hash": session, "p_rule_id": rule_id})

    async def discard(self, instance: Mapping[str, str], session: str, rule_id: str, reason: str) -> dict[str, Any]:
        return await self._rpc("approver_discard_keyword_rule_v1", {
            "p_instance": instance, "p_session_hash": session, "p_rule_id": rule_id, "p_reason": reason})


def _done(done: Mapping[str, str]) -> str:
    code = done.get("hecho")
    if not code:
        return ""
    if code in _DONE_OK:
        return f'<div class="notice notice--ok" role="status"><p>{_esc(_DONE_OK[code])}</p></div>'
    text = _DONE_WARN.get(code, "No se pudo completar. Probá de nuevo en un rato.")
    return f'<div class="notice notice--warn" role="status"><p>{_esc(text)}</p></div>'


def _actions(found: Mapping[str, Any], rule_id: str, *, csrf: str) -> str:
    status = found.get("status")
    path = f"{PREFIX}/w/{_esc(rule_id)}"
    parts: list[str] = []
    if status == "borrador" and isinstance(found.get("fingerprint"), str):
        parts.append(f'<form method="post" action="{path}/publish">{_hidden("csrf_token", csrf)}'
                     f'{_hidden("fingerprint", found.get("fingerprint"))}'
                     '<button type="submit" class="button primary">Aprobar y publicar</button></form>'
                     '<p class="meta">Queda andando sin subir nada. No le manda nada a nadie: solo pone la '
                     "etiqueta a quien empiece la conversación con este texto.</p>")
    if status == "publicada":
        parts.append(f'<form method="post" action="{path}/pause">{_hidden("csrf_token", csrf)}'
                     '<input type="text" name="motivo" maxlength="200" placeholder="Motivo (opcional)" '
                     'autocomplete="off"><button type="submit" class="button">Pausar</button></form>')
    if status == "pausada":
        parts.append(f'<form method="post" action="{path}/resume">{_hidden("csrf_token", csrf)}'
                     '<button type="submit" class="button primary">Reanudar</button></form>')
    if status in ("borrador", "pausada"):
        parts.append(f'<div class="divider"></div><form method="post" action="{path}/discard">'
                     f'{_hidden("csrf_token", csrf)}'
                     f'<input type="text" name="motivo" maxlength="{DISCARD_REASON_MAX}" '
                     'placeholder="Motivo (opcional)" autocomplete="off">'
                     '<button type="submit" class="button">Descartar</button></form>')
    return "".join(parts)


def render_rule(found: Mapping[str, Any], rule_id: str, *, serving: str | None, csrf: str = "",
                done: Mapping[str, str] | None = None, wa_me: str | None = None) -> str:
    """El cuerpo de la pagina. Sin el id, la huella ni el ID de Slack a la vista."""
    status = str(found.get("status"))
    label, chip, meaning = _STATE.get(status, ("—", None, ""))
    rule = found.get("rule") if isinstance(found.get("rule"), Mapping) else {}
    serving_html = ""
    if status == "publicada" and serving in _NOT_SERVED:
        serving_html = f'<div class="notice notice--warn"><p>{_esc(_NOT_SERVED[serving])}</p></div>'
    link_html = (f'<p class="meta">Link para el botón: <code>{_esc(wa_me)}</code></p>'
                 if isinstance(wa_me, str) and wa_me.startswith("https://wa.me/") else "")
    side = (f'<section class="panel panel--side"><div class="panel-heading"><h2>{_esc(label)}</h2>'
            f"<p>{_esc(meaning)}</p></div>"
            f'<div class="panel-body">{_actions(found, rule_id, csrf=csrf)}</div></section>')
    return (f'<header class="page-header"><p class="meta"><a href="{PREFIX}/">Aprobación de flujos</a></p>'
            f'<h1>{_esc(rule.get("nombre") or "Regla de palabra clave")}</h1>'
            f'<div class="meta">{_chip(label, chip)}</div></header>'
            f"{_done(done or {})}{serving_html}"
            '<div class="layout"><div class="stack">'
            '<section class="panel"><div class="panel-heading"><h2>Qué hace</h2></div>'
            '<div class="panel-body">'
            f'<p>Si el primer mensaje de una conversación que empieza la persona trae «{_esc(rule.get("palabra_clave"))}», '
            f'la conversación recibe la etiqueta <strong>{_esc(rule.get("etiqueta"))}</strong>.</p>'
            "<p>El agente sabe, en ese turno, que el mensaje es el texto de un link y no un pedido de hablar con "
            "una persona. No manda ningún mensaje ni arranca ningún flujo.</p>"
            f"{link_html}</div></section>"
            f"</div>{side}</div>")


def routes(*, live: rules.KeywordRules, chatwoot: Any, inbox_id: int) -> Callable[[FastAPI, PageTools], None]:
    """Las rutas, para ``register_flow_approval_routes(extra_routes=...)``."""

    def register(app: FastAPI, tools: PageTools) -> None:
        instance = tools.instance
        calls = _Calls(tools.store)

        def login() -> Response:
            return tools.logged_out(_login(None))

        def not_found(title: str) -> Response:
            return _page(title, "No existe esa regla en esta instancia.", 404)

        async def link_of(rule: Mapping[str, Any]) -> str | None:
            try:
                inbox = await chatwoot.get_inbox(inbox_id=inbox_id)
            except Exception:  # noqa: BLE001 - el link es un dato de mas
                return None
            keyword = rule.get("palabra_clave")
            return rules.wa_me_link(inbox.get("phone_number") if isinstance(inbox, Mapping) else None,
                                    keyword) if isinstance(keyword, str) else None

        @app.get(PREFIX + "/w/{rule_id}")
        async def rule_page(rule_id: str, request: Request) -> Response:
            if not _canonical_uuid(rule_id):
                return _page("Regla", "No existe esa regla.", 404)
            who = await tools.who(request)
            if who is None:
                return login()
            user, secret = who
            try:
                found = await calls.get(instance, _sha(secret), rule_id)
            except SessionInvalid:
                return login()
            except StoreRejected:
                return not_found("Regla")
            except StoreUnavailable:
                logger.info("flow_approval route=keyword_rule status=503")
                return _page("Regla", "El almacén no contestó. Probá en un rato.", 503)
            logger.info("flow_approval route=keyword_rule rule=%s status=%s", rule_id, found.get("status"))
            csrf = tools.config.csrf_token(secret)
            rule = found.get("rule") if isinstance(found.get("rule"), Mapping) else {}
            body = render_rule(found, rule_id, serving=live.serving_state(rule_id), csrf=csrf,
                               done=_done_from_query(request, tools.config, secret), wa_me=await link_of(rule))
            return HTMLResponse(_shell("Regla de palabra clave", body, user=user, csrf=csrf, show_user_id=False))

        async def run(request: Request, rule_id: str, *, title: str, fields: set[str], optional: frozenset[str],
                      call: Callable[[str, dict[str, str]], Awaitable[Mapping[str, Any]]], ok_code: str) -> Response:
            if not _canonical_uuid(rule_id):
                return _page(title, "No existe esa regla.", 404)
            checked = await tools.write_form(request, title=title, fields=fields, optional=optional)
            if isinstance(checked, Response):
                return checked
            _, secret, form = checked
            page = f"{PREFIX}/w/{rule_id}"
            try:
                result = await call(_sha(secret), form)
            except SessionInvalid:
                return login()
            except StoreRejected as rejected:
                if rejected.code == "keyword_rule_not_found":
                    return not_found(title)
                return tools.back(secret, page, hecho=rejected.code)
            except StoreUnavailable:
                return _page(title, "El almacén no contestó. Probá en un rato.", 503)
            code = ok_code if result.get("ok") is True else str(result.get("reason_code") or "keyword_rule_final")
            logger.info("flow_approval route=keyword_rule_write rule=%s title=%s result=%s", rule_id, title, code)
            if result.get("ok") is True:
                # Que se note ya, sin esperar la recarga de los 30 segundos.
                await live.refresh()
            return tools.back(secret, page, hecho=code)

        async def first_block(session: str, rule_id: str) -> str | None:
            """Lo que frena publicar o reanudar, medido ahora: choques, lo reservado y el macro."""
            found = await calls.get(instance, session, rule_id)
            try:
                rule = rules.parse_rule(found.get("rule"))
            except rules.KeywordRuleInvalid:
                return "keyword_rule_final"
            blocks = await instance_blocks(rule, live=live, chatwoot=chatwoot, rule_id=rule_id)
            return str(blocks[0]["code"]) if blocks else None

        @app.post(PREFIX + "/w/{rule_id}/publish")
        async def rule_publish(rule_id: str, request: Request) -> Response:
            async def call(session: str, form: dict[str, str]) -> Mapping[str, Any]:
                if not _FINGERPRINT.fullmatch(form["fingerprint"]):
                    return {"ok": False, "reason_code": "keyword_rule_fingerprint_invalid"}
                blocked = await first_block(session, rule_id)
                if blocked is not None:
                    return {"ok": False, "reason_code": blocked}
                return await calls.publish(instance, session, rule_id, form["fingerprint"])

            return await run(request, rule_id, title="Aprobar y publicar", fields={"csrf_token", "fingerprint"},
                             optional=frozenset(), call=call, ok_code="publicada")

        def reason_of(form: dict[str, str], default: str) -> str:
            return form.get("motivo", "").strip() or default

        @app.post(PREFIX + "/w/{rule_id}/pause")
        async def rule_pause(rule_id: str, request: Request) -> Response:
            async def call(session: str, form: dict[str, str]) -> Mapping[str, Any]:
                return await calls.pause(instance, session, rule_id, reason_of(form, PAUSE_DEFAULT_REASON))

            return await run(request, rule_id, title="Pausar", fields={"csrf_token"},
                             optional=frozenset({"motivo"}), call=call, ok_code="pausada")

        @app.post(PREFIX + "/w/{rule_id}/resume")
        async def rule_resume(rule_id: str, request: Request) -> Response:
            async def call(session: str, _: dict[str, str]) -> Mapping[str, Any]:
                blocked = await first_block(session, rule_id)
                if blocked is not None:
                    return {"ok": False, "reason_code": blocked}
                return await calls.resume(instance, session, rule_id)

            return await run(request, rule_id, title="Reanudar", fields={"csrf_token"},
                             optional=frozenset(), call=call, ok_code="reanudada")

        @app.post(PREFIX + "/w/{rule_id}/discard")
        async def rule_discard(rule_id: str, request: Request) -> Response:
            async def call(session: str, form: dict[str, str]) -> Mapping[str, Any]:
                return await calls.discard(instance, session, rule_id, reason_of(form, DISCARD_DEFAULT_REASON))

            return await run(request, rule_id, title="Descartar", fields={"csrf_token"},
                             optional=frozenset({"motivo"}), call=call, ok_code="descartada")

    return register
