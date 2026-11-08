"""La pagina de una campana del Automation Expert (agent-campaigns-v1, E-355).

Rutas, solo con ``AGENT_CAMPAIGNS_ENABLED`` (apagado no existen), con la misma
sesion de «Entrar con Slack» y la misma lista de aprobadores que la pagina de flujos:

* ``GET  /flow-approval/c/{id}``: la campana en lenguaje de negocio y sin ids a la
  vista (sale en capturas): cuando corre, quien entra, el link, lo que el agente va
  a saber, lo que dio el control de calidad y si se esta usando.
* ``POST .../publish``: «Aprobar y publicar». **El si del duenio.** Revisa que el
  ``chequeo_sha`` sea el de hoy (lo recalcula el bridge) y publica en la base; sin
  control de calidad limpio no hay boton, y la base tampoco la deja pasar.
* ``POST .../pause``, ``POST .../resume``: «Pausar» y «Reanudar», sin subir nada.
* ``POST .../discard``: «Descartar», solo lo que nunca se publico.

Lo que se publica sale de la fila validada, atada por la huella: nunca algo del
formulario. El log lleva la ruta, el id y el codigo; nunca el texto de la campana.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from . import agent_campaigns as rules
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
from .segment_check import ChequeoError, SegmentCheckError

__all__ = ["render_campaign", "routes"]

logger = logging.getLogger(__name__)

DISCARD_DEFAULT_REASON = "descartada desde la pagina de aprobacion"
PAUSE_DEFAULT_REASON = "pausada desde la pagina de aprobacion"

_STATE = {
    "borrador": ("Borrador", "chip--info", "El agente todavía la está armando."),
    "validada": ("Esperando el sí", "chip--info", "Lista para revisar. Nadie la recibe hasta que la apruebes."),
    "publicada": ("Andando", "chip--ok", "El agente la usa con quien entra a la campaña durante la vigencia."),
    "pausada": ("Pausada", "chip--warn", "El agente dejó de usarla. Podés reanudarla cuando quieras."),
    "terminada": ("Terminada", None, "Pasó la vigencia. El agente ya no la usa."),
    "vencida": ("Vencida", "chip--warn", "Pasaron las 24 horas sin aprobarla. Pedile al agente que la valide de nuevo."),
    "descartada": ("Descartada", None, "Nadie la recibió."),
}
_DONE_OK = {"publicada": "Listo: la campaña quedó andando.", "pausada": "Listo: la campaña quedó pausada.",
            "reanudada": "Listo: la campaña volvió a estar andando.", "descartada": "Descartada. Nadie la recibió."}
_DONE_WARN = {
    "agent_campaign_live_exists": "Ya hay otra campaña andando en esta instancia: pausala antes de publicar esta.",
    "agent_campaign_expired": "Venció antes de aprobarla. Pedile al agente que la valide de nuevo.",
    "agent_campaign_check_missing": "Todavía no pasó el control de calidad: no se puede publicar.",
    "agent_campaign_check_stale": ("El control de calidad ya no corresponde a lo que sabe hoy el agente. Hay que "
                                   "volver a correrlo."),
    "agent_campaign_not_validated": "Todavía no está validada: no se puede publicar.",
    "agent_campaign_window_over": "La vigencia ya terminó.",
    "agent_campaign_not_live": "La campaña no estaba andando.",
    "agent_campaign_not_paused": "La campaña no estaba pausada.",
    "agent_campaign_final": "Ya no se puede descartar: se publicó o se cerró.",
    "agent_campaign_fingerprint_mismatch": "La campaña cambió desde que la abriste. Volvé a abrirla y mirála de nuevo.",
    "agent_campaign_fingerprint_invalid": "La campaña cambió desde que la abriste. Volvé a abrirla y mirála de nuevo.",
    "flow_store_text_invalid": "El motivo no se pudo guardar: escribilo en una línea.",
}
_NOT_SERVED = {
    rules.NOT_SERVED_CHECK: ("Está aprobada, pero el agente NO la está usando: el conocimiento general cambió "
                             "después del control de calidad. Hay que volver a correrlo."),
    rules.NOT_SERVED_DRAFT: "Está aprobada, pero el agente NO la está usando: el contenido no se pudo leer.",
    rules.NOT_SERVED_CONFLICT: "Está aprobada, pero el agente NO la está usando: choca con otra campaña de la instancia.",
}


def _done(done: Mapping[str, str]) -> str:
    code = done.get("hecho")
    if not code:
        return ""
    if code in _DONE_OK:
        return f'<div class="notice notice--ok" role="status"><p>{_esc(_DONE_OK[code])}</p></div>'
    text = _DONE_WARN.get(code, "No se pudo completar. Probá de nuevo en un rato.")
    return f'<div class="notice notice--warn" role="status"><p>{_esc(text)}</p></div>'


def _check_block(found: Mapping[str, Any], *, status: str, check_stale: bool) -> tuple[str, bool]:
    """El bloque del control de calidad y si deja publicar."""
    report = found.get("check_report")
    if not isinstance(report, Mapping):
        if status in ("validada", "borrador"):
            return ('<p>Falta el control de calidad: lo corre el equipo y tarda hasta 15 minutos. Cuando esté, acá '
                    "aparece el resultado y el botón para aprobar.</p>"), False
        return "", False
    verdict = report.get("veredicto")
    if verdict == "limpio":
        if check_stale:
            return ('<div class="notice notice--warn"><p>El control de calidad dio bien, pero el conocimiento general '
                    "del agente cambió después. Hay que volver a correrlo.</p></div>"), False
        return "<p>Pasó el control de calidad: no choca con lo que el agente ya sabe.</p>", True
    if verdict == "sin_veredicto":
        return '<div class="notice notice--warn"><p>El control de calidad no llegó a un resultado. Hay que volver a correrlo.</p></div>', False
    items = report.get("items")
    lines = "".join(f"<li>{_esc(item.get('pregunta'))}</li>" for item in items if isinstance(item, Mapping)) \
        if isinstance(items, list) else ""
    return ('<div class="notice notice--warn"><p>El control de calidad dejó preguntas. Contestalas con el agente: '
            f"la vuelve a armar y se corre otra vez.</p><ul>{lines}</ul></div>"), False


def _actions(found: Mapping[str, Any], campaign_id: str, *, csrf: str, can_publish: bool) -> str:
    status = found.get("effective_status")
    path = f"{PREFIX}/c/{_esc(campaign_id)}"
    parts: list[str] = []
    if status == "validada" and can_publish and isinstance(found.get("fingerprint"), str):
        parts.append(f'<form method="post" action="{path}/publish">{_hidden("csrf_token", csrf)}'
                     f'{_hidden("fingerprint", found.get("fingerprint"))}'
                     '<button type="submit" class="button primary">Aprobar y publicar</button></form>'
                     '<p class="meta">Queda andando sin subir nada. No le manda nada a nadie: el agente la usa con '
                     "quien escriba con la palabra clave o conteste que sí a la pregunta.</p>")
    if status == "publicada":
        parts.append(f'<form method="post" action="{path}/pause">{_hidden("csrf_token", csrf)}'
                     '<input type="text" name="motivo" maxlength="200" placeholder="Motivo (opcional)" '
                     'autocomplete="off"><button type="submit" class="button">Pausar</button></form>')
    if status == "pausada":
        parts.append(f'<form method="post" action="{path}/resume">{_hidden("csrf_token", csrf)}'
                     '<button type="submit" class="button primary">Reanudar</button></form>')
    if status in ("borrador", "validada"):
        parts.append(f'<div class="divider"></div><form method="post" action="{path}/discard">'
                     f'{_hidden("csrf_token", csrf)}'
                     f'<input type="text" name="motivo" maxlength="{DISCARD_REASON_MAX}" '
                     'placeholder="Motivo (opcional)" autocomplete="off">'
                     '<button type="submit" class="button">Descartar</button></form>')
    return "".join(parts)


def _entry_lines(draft: Mapping[str, Any], preview: Mapping[str, Any]) -> str:
    lines = ""
    keywords = draft.get("palabras_clave")
    if isinstance(keywords, list) and keywords:
        said = ", ".join(f"«{_esc(keyword)}»" for keyword in keywords)
        lines += f"<li>Quien empieza la conversación con {said}.</li>"
    if draft.get("pregunta_entrada"):
        lines += f"<li>Quien escribe por su cuenta y contesta que sí a «{_esc(draft.get('pregunta_entrada'))}».</li>"
    link = preview.get("wa_me")
    link_html = ""
    if isinstance(link, str) and link.startswith("https://wa.me/"):
        link_html = f'<p class="meta">Link para el botón de la landing: <code>{_esc(link)}</code></p>'
    return f'<ul class="meta">{lines}</ul>{link_html}'


def render_campaign(found: Mapping[str, Any], campaign_id: str, *, serving: str | None, can_publish: bool,
                    check_stale: bool, csrf: str = "", done: Mapping[str, str] | None = None,
                    time_zone: str | None = None) -> str:
    """El cuerpo de la pagina. Sin el id, la huella ni el ID de Slack a la vista."""
    status = str(found.get("effective_status") or found.get("status"))
    label, chip, meaning = _STATE.get(status, ("—", None, ""))
    draft = found.get("draft") if isinstance(found.get("draft"), Mapping) else {}
    validation = found.get("validation") if isinstance(found.get("validation"), Mapping) else {}
    preview = validation.get("preview") if isinstance(validation.get("preview"), Mapping) else {}
    rendered = preview.get("render")
    check_html, can_click = _check_block(found, status=status, check_stale=check_stale)
    can_click = can_click and can_publish
    serving_html = ""
    if status == "publicada" and serving in _NOT_SERVED:
        serving_html = f'<div class="notice notice--warn"><p>{_esc(_NOT_SERVED[serving])}</p></div>'
    coupon = draft.get("oferta", {}).get("cupon") if isinstance(draft.get("oferta"), Mapping) else None
    coupon_html = (f'<p>El link de pago sale con el cupón <strong>{_esc(coupon)}</strong>. '
                   "Confirmá que exista en Hotmart y que dure toda la campaña: esto no se puede verificar desde acá.</p>"
                   if coupon else "")
    window = (f"Del {_esc(_when(found.get('valid_from'), time_zone))} al "
              f"{_esc(_when(found.get('valid_until'), time_zone))}.")
    side = (f'<section class="panel panel--side"><div class="panel-heading"><h2>{_esc(label)}</h2>'
            f"<p>{_esc(meaning)}</p></div>"
            f'<div class="panel-body">{check_html}'
            f'{_actions(found, campaign_id, csrf=csrf, can_publish=can_click)}</div></section>')
    return (f'<header class="page-header"><p class="meta"><a href="{PREFIX}/">Aprobación de flujos</a></p>'
            f'<h1>{_esc(draft.get("nombre") or "Campaña")}</h1><div class="meta">{_chip(label, chip)}</div></header>'
            f"{_done(done or {})}{serving_html}"
            '<div class="layout"><div class="stack">'
            f'<section class="panel"><div class="panel-heading"><h2>Cuándo corre y quién entra</h2></div>'
            f'<div class="panel-body"><p>{window}</p>{_entry_lines(draft, preview)}{coupon_html}</div></section>'
            '<section class="panel"><div class="panel-heading"><h2>Lo que el agente va a saber</h2></div>'
            f'<div class="panel-body"><pre class="bubble">{_esc(rendered)}</pre></div></section>'
            f"</div>{side}</div>")


def routes(*, live: rules.LiveSegmentCatalog) -> Callable[[FastAPI, PageTools], None]:
    """Las rutas, para ``register_flow_approval_routes(extra_routes=...)``."""

    def expected(found: Mapping[str, Any]) -> str | None:
        """El chequeo_sha de hoy para esta fila, o None si la fila ya no se puede armar."""
        try:
            draft = rules.parse_campaign(found.get("draft"))
            return rules.expected_check_sha(draft, version=int(found.get("segment_version") or 0), context=live.context)
        except (rules.CampaignInvalid, ChequeoError, SegmentCheckError, TypeError, ValueError):
            return None

    def register(app: FastAPI, tools: PageTools) -> None:
        store, instance = tools.store, tools.instance

        def login(campaign_id: str) -> Response:
            return tools.logged_out(_login(None, campaign=campaign_id))

        def not_found(title: str) -> Response:
            return _page(title, "No existe esa campaña en esta instancia.", 404)

        @app.get(PREFIX + "/c/{campaign_id}")
        async def campaign_page(campaign_id: str, request: Request) -> Response:
            if not _canonical_uuid(campaign_id):
                return _page("Campaña", "No existe esa campaña.", 404)
            who = await tools.who(request)
            if who is None:
                return login(campaign_id)
            user, secret = who
            try:
                found = await store.get_campaign(instance, session_hash=_sha(secret), campaign_id=campaign_id)
            except SessionInvalid:
                return login(campaign_id)
            except StoreRejected:
                return not_found("Campaña")
            except StoreUnavailable:
                logger.info("flow_approval route=campaign status=503")
                return _page("Campaña", "El almacén no contestó. Probá en un rato.", 503)
            logger.info("flow_approval route=campaign campaign=%s status=%s", campaign_id, found.get("status"))
            sha = found.get("check_sha")
            stale = isinstance(sha, str) and sha != expected(found)
            csrf = tools.config.csrf_token(secret)
            body = render_campaign(found, campaign_id, serving=live.serving_state(campaign_id),
                                   can_publish=isinstance(sha, str) and not stale, check_stale=stale, csrf=csrf,
                                   done=_done_from_query(request, tools.config, secret), time_zone=tools.time_zone)
            return HTMLResponse(_shell("Campaña", body, user=user, csrf=csrf, show_user_id=False))

        async def run(request: Request, campaign_id: str, *, title: str, fields: set[str], optional: frozenset[str],
                      call: Callable[[str, dict[str, str]], Any], ok_code: str) -> Response:
            if not _canonical_uuid(campaign_id):
                return _page(title, "No existe esa campaña.", 404)
            checked = await tools.write_form(request, title=title, fields=fields, optional=optional,
                                             campaign=campaign_id)
            if isinstance(checked, Response):
                return checked
            _, secret, form = checked
            page = f"{PREFIX}/c/{campaign_id}"
            try:
                result = await call(_sha(secret), form)
            except SessionInvalid:
                return login(campaign_id)
            except StoreRejected as rejected:
                if rejected.code == "agent_campaign_not_found":
                    return not_found(title)
                return tools.back(secret, page, hecho=rejected.code)
            except StoreUnavailable:
                return _page(title, "El almacén no contestó. Probá en un rato.", 503)
            code = ok_code if result.get("ok") is True else str(result.get("reason_code") or "agent_campaign_final")
            logger.info("flow_approval route=campaign_write campaign=%s title=%s result=%s", campaign_id, title, code)
            if result.get("ok") is True:
                # Que se note ya, sin esperar la recarga de los 30 segundos.
                await live.refresh()
            return tools.back(secret, page, hecho=code)

        @app.post(PREFIX + "/c/{campaign_id}/publish")
        async def campaign_publish(campaign_id: str, request: Request) -> Response:
            async def call(session: str, form: dict[str, str]) -> Mapping[str, Any]:
                if not _FINGERPRINT.fullmatch(form["fingerprint"]):
                    return {"ok": False, "reason_code": "agent_campaign_fingerprint_invalid"}
                found = await store.get_campaign(instance, session_hash=session, campaign_id=campaign_id)
                sha = found.get("check_sha")
                if not isinstance(sha, str):
                    return {"ok": False, "reason_code": "agent_campaign_check_missing"}
                if sha != expected(found):
                    return {"ok": False, "reason_code": "agent_campaign_check_stale"}
                return await store.publish_campaign(instance, session_hash=session, campaign_id=campaign_id,
                                                    fingerprint=form["fingerprint"], check_sha=sha)

            return await run(request, campaign_id, title="Aprobar y publicar", fields={"csrf_token", "fingerprint"},
                             optional=frozenset(), call=call, ok_code="publicada")

        def reason_of(form: dict[str, str], default: str) -> str:
            return form.get("motivo", "").strip() or default

        @app.post(PREFIX + "/c/{campaign_id}/pause")
        async def campaign_pause(campaign_id: str, request: Request) -> Response:
            async def call(session: str, form: dict[str, str]) -> Mapping[str, Any]:
                return await store.pause_campaign(instance, session_hash=session, campaign_id=campaign_id,
                                                  reason=reason_of(form, PAUSE_DEFAULT_REASON))

            return await run(request, campaign_id, title="Pausar", fields={"csrf_token"},
                             optional=frozenset({"motivo"}), call=call, ok_code="pausada")

        @app.post(PREFIX + "/c/{campaign_id}/resume")
        async def campaign_resume(campaign_id: str, request: Request) -> Response:
            async def call(session: str, _: dict[str, str]) -> Mapping[str, Any]:
                return await store.resume_campaign(instance, session_hash=session, campaign_id=campaign_id)

            return await run(request, campaign_id, title="Reanudar", fields={"csrf_token"},
                             optional=frozenset(), call=call, ok_code="reanudada")

        @app.post(PREFIX + "/c/{campaign_id}/discard")
        async def campaign_discard(campaign_id: str, request: Request) -> Response:
            async def call(session: str, form: dict[str, str]) -> Mapping[str, Any]:
                return await store.discard_campaign(instance, session_hash=session, campaign_id=campaign_id,
                                                    reason=reason_of(form, DISCARD_DEFAULT_REASON))

            return await run(request, campaign_id, title="Descartar", fields={"csrf_token"},
                             optional=frozenset({"motivo"}), call=call, ok_code="descartada")

    return register
