"""La pagina de una instruccion temporal (agent-temporary-instruction-v1).

Rutas, solo con ``AGENT_TEMPORARY_INSTRUCTION_ENABLED`` (apagado no existen), con
la misma sesion de «Entrar con Slack» y la misma lista de aprobadores que la
pagina de flujos y la de campanas:

* ``GET  /flow-approval/i/{id}``: la instruccion en lenguaje de negocio y sin ids
  a la vista: hasta cuando vale, lo que el agente va a leer y si se esta usando.
* ``POST .../publish``: «Aprobar y publicar». **El si.** Con otra viva, la avisa
  antes del boton: publicar esta la retira (D-53.3).
* ``POST .../retire``: «Retirar», antes del ``hasta`` (D-53.4).
* ``POST .../discard``: «Descartar», solo lo que nunca se publico.

Lo que se publica sale de la fila, atada por la huella: nunca algo del
formulario. El log lleva la ruta, el id y el codigo; nunca el texto.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from . import temporary_instruction as rules
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

__all__ = ["render_instruction", "routes"]

logger = logging.getLogger(__name__)

DISCARD_DEFAULT_REASON = "descartada desde la pagina de aprobacion"
RETIRE_DEFAULT_REASON = "retirada desde la pagina de aprobacion"
TITLE = "Instrucción temporal"

_STATE = {
    "validada": ("Esperando el sí", "chip--info", "Lista para revisar. El agente no la usa hasta que la apruebes."),
    "publicada": ("Andando", "chip--ok", "El agente la tiene en cuenta en todas las conversaciones hasta el vencimiento."),
    "terminada": ("Terminada", None, "Llegó la fecha de vencimiento. El agente ya no la usa."),
    "retirada": ("Retirada", None, "El agente ya no la usa."),
    "vencida": ("Vencida", "chip--warn", "Pasaron las 24 horas sin aprobarla. Pedile al agente que la proponga de nuevo."),
    "descartada": ("Descartada", None, "El agente nunca la usó."),
}
_DONE_OK = {"publicada": "Listo: la instrucción quedó andando.", "retirada": "Listo: la instrucción quedó retirada.",
            "descartada": "Descartada. El agente nunca la usó."}
_DONE_WARN = {
    "agent_temporary_instruction_expired": "Venció antes de aprobarla. Pedile al agente que la proponga de nuevo.",
    "agent_temporary_instruction_window_over": "Ya pasó la fecha hasta la que valía.",
    "agent_temporary_instruction_not_validated": "Ya no está esperando el sí: no se puede publicar.",
    "agent_temporary_instruction_not_live": "La instrucción no estaba andando.",
    "agent_temporary_instruction_final": "Ya no se puede descartar: se publicó o se cerró.",
    "agent_temporary_instruction_fingerprint_mismatch": ("La instrucción cambió desde que la abriste. Volvé a "
                                                         "abrirla y mirala de nuevo."),
    "agent_temporary_instruction_fingerprint_invalid": ("La instrucción cambió desde que la abriste. Volvé a "
                                                        "abrirla y mirala de nuevo."),
    "flow_store_text_invalid": "El motivo no se pudo guardar: escribilo en una línea.",
}
_NOT_SERVED = {
    rules.NOT_SERVED_CONTENT: ("Está aprobada, pero el agente NO la está usando: el texto ya no pasa las reglas. "
                               "Retirala y pedile al agente una nueva."),
}


def _done(done: Mapping[str, str]) -> str:
    code = done.get("hecho")
    if not code:
        return ""
    if code in _DONE_OK:
        return f'<div class="notice notice--ok" role="status"><p>{_esc(_DONE_OK[code])}</p></div>'
    text = _DONE_WARN.get(code, "No se pudo completar. Probá de nuevo en un rato.")
    return f'<div class="notice notice--warn" role="status"><p>{_esc(text)}</p></div>'


def _until(value: object, time_zone: str | None) -> str:
    try:
        return rules.when_text(rules.parse_until(str(value).replace(" ", "T", 1)), time_zone)
    except rules.InstructionInvalid:
        return "—"


def _actions(found: Mapping[str, Any], instruction_id: str, *, csrf: str, time_zone: str | None) -> str:
    status = found.get("effective_status")
    path = f"{PREFIX}/i/{_esc(instruction_id)}"
    parts: list[str] = []
    if status == "validada" and isinstance(found.get("fingerprint"), str):
        other = found.get("other_live")
        if isinstance(other, Mapping):
            parts.append('<div class="notice notice--warn"><p>Ahora hay otra instrucción andando (hasta el '
                         f"{_esc(_until(other.get('valid_until'), time_zone))}). Si publicás esta, aquella deja "
                         "de usarse.</p></div>")
        parts.append(f'<form method="post" action="{path}/publish">{_hidden("csrf_token", csrf)}'
                     f'{_hidden("fingerprint", found.get("fingerprint"))}'
                     '<button type="submit" class="button primary">Aprobar y publicar</button></form>'
                     '<p class="meta">Queda andando sin subir nada. No le manda nada a nadie: el agente la tiene en '
                     "cuenta cuando contesta, hasta el vencimiento.</p>")
        parts.append(f'<div class="divider"></div><form method="post" action="{path}/discard">'
                     f'{_hidden("csrf_token", csrf)}'
                     f'<input type="text" name="motivo" maxlength="{DISCARD_REASON_MAX}" '
                     'placeholder="Motivo (opcional)" autocomplete="off">'
                     '<button type="submit" class="button">Descartar</button></form>')
    if status == "publicada":
        parts.append(f'<form method="post" action="{path}/retire">{_hidden("csrf_token", csrf)}'
                     '<input type="text" name="motivo" maxlength="200" placeholder="Motivo (opcional)" '
                     'autocomplete="off"><button type="submit" class="button">Retirar</button></form>'
                     '<p class="meta">El agente la deja de usar en menos de un minuto.</p>')
    return "".join(parts)


def render_instruction(found: Mapping[str, Any], instruction_id: str, *, serving: str | None, csrf: str = "",
                       done: Mapping[str, str] | None = None, time_zone: str | None = None) -> str:
    """El cuerpo de la pagina. Sin el id, la huella ni el ID de Slack a la vista."""
    status = str(found.get("effective_status") or found.get("status"))
    label, chip, meaning = _STATE.get(status, ("—", None, ""))
    text = found.get("texto")
    try:
        rendered = rules.render(rules.parse_text(text), rules.parse_until(str(found.get("valid_until")).replace(
            " ", "T", 1)), time_zone=time_zone)
    except rules.InstructionInvalid:
        rendered = text if isinstance(text, str) else ""
    serving_html = ""
    if status == "publicada" and serving in _NOT_SERVED:
        serving_html = f'<div class="notice notice--warn"><p>{_esc(_NOT_SERVED[serving])}</p></div>'
    side = (f'<section class="panel panel--side"><div class="panel-heading"><h2>{_esc(label)}</h2>'
            f"<p>{_esc(meaning)}</p></div>"
            f'<div class="panel-body">{_actions(found, instruction_id, csrf=csrf, time_zone=time_zone)}</div>'
            "</section>")
    return (f'<header class="page-header"><p class="meta"><a href="{PREFIX}/">Aprobación de flujos</a></p>'
            f'<h1>{_esc(TITLE)}</h1><div class="meta">{_chip(label, chip)}</div></header>'
            f"{_done(done or {})}{serving_html}"
            '<div class="layout"><div class="stack">'
            '<section class="panel"><div class="panel-heading"><h2>Hasta cuándo vale</h2></div>'
            f'<div class="panel-body"><p>Hasta el {_esc(_until(found.get("valid_until"), time_zone))}. Después el '
            "agente la deja de usar solo.</p><p>Vale para todas las conversaciones.</p></div></section>"
            '<section class="panel"><div class="panel-heading"><h2>Lo que el agente va a leer</h2></div>'
            f'<div class="panel-body"><pre class="bubble">{_esc(rendered)}</pre></div></section>'
            f"</div>{side}</div>")


def routes(*, live: rules.LiveTemporaryInstruction,
           now: Callable[[], datetime] = rules.utc_now) -> Callable[[FastAPI, PageTools], None]:
    """Las rutas, para ``register_flow_approval_routes(extra_routes=...)``."""

    def register(app: FastAPI, tools: PageTools) -> None:
        store, instance = tools.store, tools.instance

        def login(instruction_id: str) -> Response:
            return tools.logged_out(_login(None, instruction=instruction_id))

        def not_found() -> Response:
            return _page(TITLE, "No existe esa instrucción en esta instancia.", 404)

        @app.get(PREFIX + "/i/{instruction_id}")
        async def instruction_page(instruction_id: str, request: Request) -> Response:
            if not _canonical_uuid(instruction_id):
                return _page(TITLE, "No existe esa instrucción.", 404)
            who = await tools.who(request)
            if who is None:
                return login(instruction_id)
            user, secret = who
            try:
                found = await store.get_temporary_instruction(instance, session_hash=_sha(secret),
                                                              instruction_id=instruction_id)
            except SessionInvalid:
                return login(instruction_id)
            except StoreRejected:
                return not_found()
            except StoreUnavailable:
                logger.info("flow_approval route=temporary_instruction status=503")
                return _page(TITLE, "El almacén no contestó. Probá en un rato.", 503)
            logger.info("flow_approval route=temporary_instruction id=%s status=%s", instruction_id,
                        found.get("status"))
            csrf = tools.config.csrf_token(secret)
            body = render_instruction(found, instruction_id, serving=live.serving_state(instruction_id, now()),
                                      csrf=csrf, done=_done_from_query(request, tools.config, secret),
                                      time_zone=tools.time_zone)
            return HTMLResponse(_shell(TITLE, body, user=user, csrf=csrf, show_user_id=False))

        async def run(request: Request, instruction_id: str, *, title: str, fields: set[str],
                      optional: frozenset[str], call: Callable[[str, dict[str, str]], Any], ok_code: str) -> Response:
            if not _canonical_uuid(instruction_id):
                return _page(title, "No existe esa instrucción.", 404)
            checked = await tools.write_form(request, title=title, fields=fields, optional=optional,
                                             instruction=instruction_id)
            if isinstance(checked, Response):
                return checked
            _, secret, form = checked
            page = f"{PREFIX}/i/{instruction_id}"
            try:
                result = await call(_sha(secret), form)
            except SessionInvalid:
                return login(instruction_id)
            except StoreRejected as rejected:
                if rejected.code == "agent_temporary_instruction_not_found":
                    return not_found()
                return tools.back(secret, page, hecho=rejected.code)
            except StoreUnavailable:
                return _page(title, "El almacén no contestó. Probá en un rato.", 503)
            code = ok_code if result.get("ok") is True else str(
                result.get("reason_code") or "agent_temporary_instruction_final")
            logger.info("flow_approval route=temporary_instruction_write id=%s title=%s result=%s", instruction_id,
                        title, code)
            if result.get("ok") is True:
                # Que se note ya, sin esperar la recarga de los 30 segundos.
                await live.refresh()
            return tools.back(secret, page, hecho=code)

        def reason_of(form: dict[str, str], default: str) -> str:
            return form.get("motivo", "").strip() or default

        @app.post(PREFIX + "/i/{instruction_id}/publish")
        async def instruction_publish(instruction_id: str, request: Request) -> Response:
            async def call(session: str, form: dict[str, str]) -> Mapping[str, Any]:
                if not _FINGERPRINT.fullmatch(form["fingerprint"]):
                    return {"ok": False, "reason_code": "agent_temporary_instruction_fingerprint_invalid"}
                return await store.publish_temporary_instruction(instance, session_hash=session,
                                                                 instruction_id=instruction_id,
                                                                 fingerprint=form["fingerprint"])

            return await run(request, instruction_id, title="Aprobar y publicar",
                             fields={"csrf_token", "fingerprint"}, optional=frozenset(), call=call,
                             ok_code="publicada")

        @app.post(PREFIX + "/i/{instruction_id}/retire")
        async def instruction_retire(instruction_id: str, request: Request) -> Response:
            async def call(session: str, form: dict[str, str]) -> Mapping[str, Any]:
                return await store.retire_temporary_instruction(instance, session_hash=session,
                                                                instruction_id=instruction_id,
                                                                reason=reason_of(form, RETIRE_DEFAULT_REASON))

            return await run(request, instruction_id, title="Retirar", fields={"csrf_token"},
                             optional=frozenset({"motivo"}), call=call, ok_code="retirada")

        @app.post(PREFIX + "/i/{instruction_id}/discard")
        async def instruction_discard(instruction_id: str, request: Request) -> Response:
            async def call(session: str, form: dict[str, str]) -> Mapping[str, Any]:
                return await store.discard_temporary_instruction(instance, session_hash=session,
                                                                 instruction_id=instruction_id,
                                                                 reason=reason_of(form, DISCARD_DEFAULT_REASON))

            return await run(request, instruction_id, title="Descartar", fields={"csrf_token"},
                             optional=frozenset({"motivo"}), call=call, ok_code="descartada")

    return register
