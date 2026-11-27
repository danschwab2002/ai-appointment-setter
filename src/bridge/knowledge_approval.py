"""La pagina de una version del conocimiento vivo (live-knowledge-v1, pieza 5.5).

Rutas, solo con ``AGENT_LIVE_KNOWLEDGE_ENABLED`` (apagado no existen), con la misma sesion
de «Entrar con Slack» y la misma lista de aprobadores que la pagina de flujos:

* ``GET  /flow-approval/k/{id}``: la version en lenguaje de negocio: para que es, que
  secciones toca, el diff contra lo vigente y si el agente la esta usando.
* ``POST .../publish``: «Aprobar y usar». **El si.** El bridge rehace la version desde la
  fila, recalcula el hash y las reglas, y revisa que lo vigente sea aquello sobre lo que
  se armo y que no haya un segmento en vigencia (D-55.4). La base exige la huella.
* ``POST .../discard``: «Descartar», solo una validada.
* ``POST .../return-to-file``: «Volver al archivo»: retira la viva publicada.
* ``POST .../harness`` (solo con ``AGENT_LIVE_KNOWLEDGE_HARNESS_ENABLED``): «Volver a correr el
  arnés», solo si la corrida de esa version dio error o se abandono (D-55.14).

Con ``AGENT_LIVE_KNOWLEDGE_HARNESS_ENABLED`` la pagina muestra el resultado del arnes F-055 sobre
esa version (``knowledge_harness``) y «Aprobar y usar» no aparece hasta que de verde para el mismo
texto; el boton lo vuelve a mirar al apretarlo (D-55.11). Sin el flag, la pagina es la de antes.

Lo que se publica sale de la fila validada, atada por la huella: nunca algo del
formulario. El log lleva la ruta, el id y el codigo; nunca el texto.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from . import live_knowledge as rules
from .commercial_knowledge import KnowledgeError
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

__all__ = ["publish_refusal", "render_knowledge", "routes"]

logger = logging.getLogger(__name__)

DISCARD_DEFAULT_REASON = "descartada desde la pagina de aprobacion"
RETURN_DEFAULT_REASON = "vuelta al archivo desde la pagina de aprobacion"

_STATE = {
    "validada": ("Esperando el sí", "chip--info", "Lista para revisar. El agente no la usa hasta que la apruebes."),
    "publicada": ("En uso", "chip--ok", "El agente la usa en todas las conversaciones."),
    "retirada": ("Retirada", None, "El agente ya no la usa: se aprobó otra o se volvió al archivo."),
    "vencida": ("Vencida", "chip--warn", "Pasaron las 24 horas sin aprobarla. Pedile al agente que la arme de nuevo."),
    "descartada": ("Descartada", None, "Nadie la recibió."),
}
_DONE_OK = {"publicada": "Listo: el agente ya usa esta versión.",
            "descartada": "Descartada. El agente no la recibió.",
            "archivo": "Listo: el agente volvió al conocimiento del archivo."}
_DONE_WARN = {
    "agent_knowledge_expired": "Venció antes de aprobarla. Pedile al agente que la arme de nuevo.",
    "agent_knowledge_not_validated": "Ya no está esperando el sí.",
    "agent_knowledge_final": "Ya no se puede descartar: se aprobó o se cerró.",
    "agent_knowledge_fingerprint_mismatch": "La versión cambió desde que la abriste. Volvé a abrirla.",
    "agent_knowledge_fingerprint_invalid": "La versión cambió desde que la abriste. Volvé a abrirla.",
    "agent_knowledge_base_changed": ("Lo que sabe hoy el agente cambió desde que se armó esta versión (se aprobó "
                                     "otra o se volvió al archivo). Pedile al agente que la arme de nuevo."),
    "agent_knowledge_file_changed": ("El conocimiento del archivo de la instancia cambió desde que se armó esta "
                                     "versión. Pedile al agente que la arme de nuevo."),
    "agent_knowledge_render_changed": ("Esta versión del bridge no produce el mismo texto que se validó. Pedile al "
                                       "agente que la arme de nuevo."),
    "agent_knowledge_rules_failed": "Ya no pasa las reglas de la instancia (precio o productos). No se puede aprobar.",
    "knowledge_segments_in_force": ("Hay una campaña o un segmento en vigencia que se revisó contra el conocimiento "
                                    "de hoy: no se puede cambiar mientras dure. Para algo urgente, usá la "
                                    "instrucción temporal."),
    "agent_knowledge_no_live": "No había ninguna versión en uso: el agente ya usa el archivo.",
    # 5.5 parte 2: el arnés F-055 (D-55.11). Sin verde para este mismo texto no se aprueba.
    "knowledge_harness_not_run": ("El arnés de conversaciones todavía no corrió sobre esta versión: no se puede "
                                  "aprobar hasta que dé verde."),
    "knowledge_harness_running": "El arnés de conversaciones está corriendo. Volvé a abrir la página en unos minutos.",
    "knowledge_harness_red": ("El arnés encontró respuestas que esta versión empeora respecto de lo vigente: no se "
                              "puede aprobar. Pedile al agente que la corrija."),
    "knowledge_harness_error": ("El arnés no pudo terminar (el modelo no contestó o se reinició el bridge): no se "
                                "puede aprobar hasta que corra de nuevo y dé verde."),
    "knowledge_harness_busy": "Ya hay otra corrida del arnés en curso. Probá en unos minutos.",
    "knowledge_harness_daily_limit": "Ya se corrió el arnés las veces que permite el día. Probá mañana.",
    "knowledge_harness_already_ran": "El arnés ya dio un resultado para esta versión: no se vuelve a correr.",
    "knowledge_harness_store_unavailable": "No se pudo guardar la corrida del arnés. Probá en un rato.",
    "knowledge_harness_started": "Listo: el arnés empezó a correr. Volvé a abrir la página en unos minutos.",
    "flow_store_text_invalid": "El motivo no se pudo guardar: escribilo en una línea.",
}
_NOT_SERVED = {
    rules.NOT_SERVED_SEGMENTS: ("Está aprobada, pero el agente NO la está usando mientras haya una campaña o un "
                                "segmento en vigencia: usa el conocimiento del archivo, contra el que se revisó. "
                                "Vuelve sola cuando termine."),
    rules.NOT_SERVED_FILE_CHANGED: ("Está aprobada, pero el agente NO la está usando: el conocimiento del archivo "
                                    "cambió después. Usá «Volver al archivo» y pedile al agente que la arme de nuevo."),
    rules.NOT_SERVED_RENDER: ("Está aprobada, pero el agente NO la está usando: esta versión del bridge no produce "
                              "el texto aprobado."),
    rules.NOT_SERVED_INVALID: "Está aprobada, pero el agente NO la está usando: ya no pasa las reglas de la instancia.",
}
_FINDING = {
    "knowledge_harness_not_run": "El arnés de conversaciones no corrió: conviene probarla con el equipo después.",
    "knowledge_near_size_limit": "Quedó cerca del tamaño máximo que puede recibir el agente.",
}


def _done(done: Mapping[str, str]) -> str:
    code = done.get("hecho")
    if not code:
        return ""
    if code == "knowledge_harness_started":
        return f'<div class="notice notice--ok" role="status"><p>{_esc(_DONE_WARN[code])}</p></div>'
    if code in _DONE_OK:
        return f'<div class="notice notice--ok" role="status"><p>{_esc(_DONE_OK[code])}</p></div>'
    text = _DONE_WARN.get(code, "No se pudo completar. Probá de nuevo en un rato.")
    return f'<div class="notice notice--warn" role="status"><p>{_esc(text)}</p></div>'


def _diff_html(preview: Mapping[str, Any]) -> str:
    diff = preview.get("diff") if isinstance(preview.get("diff"), Mapping) else {}
    lines = diff.get("lineas") if isinstance(diff.get("lineas"), list) else []
    prefix = {"agrega": "+ ", "saca": "- ", "igual": "  ", "salto": "…"}
    text = "\n".join(prefix.get(str(line.get("tipo")), "  ") + str(line.get("texto") or "")
                     for line in lines if isinstance(line, Mapping))
    cut = '<p class="meta">El cambio es más largo: se muestra el principio.</p>' if diff.get("truncado") else ""
    summary = (f'<p class="meta">{_esc(diff.get("agregadas"))} líneas nuevas y {_esc(diff.get("sacadas"))} '
               "que salen. Las que empiezan con + son lo nuevo; con -, lo que deja de decir.</p>")
    return f'{summary}<pre class="bubble">{_esc(text)}</pre>{cut}'


_HARNESS_STATE = {
    "corriendo": ("Corriendo", "chip--info", "Está probando las respuestas de esta versión contra las de lo vigente."),
    "verde": ("Verde", "chip--ok", "Ninguna respuesta empeora respecto de lo vigente."),
    "rojo": ("Rojo", "chip--warn", "Hay respuestas que esta versión empeora en todas las pruebas."),
    "error": ("Sin resultado", "chip--warn", "No pudo terminar: hay que correrlo de nuevo."),
}


def _harness_cases(title: str, items: Any) -> str:
    rows = [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []
    if not rows:
        return ""
    shown = "".join(
        f'<li><p><strong>{_esc(item.get("id"))}</strong>: {_esc(item.get("espera"))}</p>'
        f'<p class="meta">Falla en el borrador {_esc(item.get("borrador_falla"))}, en lo vigente '
        f'{_esc(item.get("vigente_falla"))}. {_esc(", ".join(str(x) for x in item.get("fallas") or []))}</p>'
        + (f'<pre class="bubble">{_esc(item.get("respuesta"))}</pre>' if item.get("respuesta") else "")
        + "</li>" for item in rows)
    return f"<h3>{_esc(title)}</h3><ul>{shown}</ul>"


def _harness_html(harness: Mapping[str, Any] | None, *, rerun: str) -> str:
    """El panel del arnés F-055 (5.5 parte 2). ``rerun`` es el formulario para volver a correrlo, o vacío."""
    if harness is None:
        body = ('<p>Todavía no corrió sobre esta versión. Sin el arnés en verde no se puede aprobar.</p>'
                f"{rerun}")
        return (f'<section class="panel"><div class="panel-heading"><h2>Arnés de conversaciones</h2></div>'
                f'<div class="panel-body">{body}</div></section>')
    status = str(harness.get("status"))
    label, chip, meaning = _HARNESS_STATE.get(status, ("—", None, ""))
    result = harness.get("result") if isinstance(harness.get("result"), Mapping) else {}
    counts = ""
    if result:
        counts = (f'<p class="meta">{_esc(result.get("casos"))} casos, {_esc(result.get("repeticiones"))} veces cada '
                  f"uno contra lo vigente y contra esta versión ({_esc(result.get('llamadas'))} respuestas, "
                  f"{_esc(result.get('sin_respuesta'))} sin respuesta del modelo).</p>")
    body = (f"<p>{_chip(label, chip)} {_esc(meaning)}</p>{counts}"
            + _harness_cases("Lo que empeora (frena)", result.get("regresiones"))
            + _harness_cases("Para mirar (no frena: las reglas fijas tienen falsos positivos)", result.get("avisos"))
            + _harness_cases("Para leer siempre", result.get("para_leer"))
            + rerun)
    return (f'<section class="panel"><div class="panel-heading"><h2>Arnés de conversaciones</h2></div>'
            f'<div class="panel-body">{body}</div></section>')


def render_knowledge(found: Mapping[str, Any], knowledge_id: str, *, serving: str | None, refusal: str | None,
                     csrf: str = "", done: Mapping[str, str] | None = None, time_zone: str | None = None,
                     harness: Mapping[str, Any] | None = None, harness_enabled: bool = False,
                     harness_rerun: bool = False) -> str:
    """El cuerpo de la pagina. Sin el id, la huella ni el ID de Slack a la vista.

    ``harness_enabled`` (5.5 parte 2): suma el panel del arnés con ``harness`` (None si no corrió) y, con
    ``harness_rerun``, el botón para volver a correrlo. Sin el flag, la página de antes."""
    status = str(found.get("effective_status") or found.get("status"))
    label, chip, meaning = _STATE.get(status, ("—", None, ""))
    preview = found.get("preview") if isinstance(found.get("preview"), Mapping) else {}
    sections = preview.get("secciones") if isinstance(preview.get("secciones"), list) else []
    base = preview.get("base") if isinstance(preview.get("base"), Mapping) else {}
    base_text = ("el conocimiento del archivo" if base.get("origen") == rules.ORIGIN_FILE
                 else f"la versión viva {_esc(base.get('live_version'))}")
    warnings = preview.get("warnings") if isinstance(preview.get("warnings"), list) else []
    warn_html = "".join(f"<li>{_esc(_FINDING.get(str(item.get('code')), item.get('code')))}</li>"
                        for item in warnings if isinstance(item, Mapping))
    serving_html = ""
    if status == "publicada" and serving in _NOT_SERVED:
        serving_html = f'<div class="notice notice--warn"><p>{_esc(_NOT_SERVED[serving])}</p></div>'
    path = f"{PREFIX}/k/{_esc(knowledge_id)}"
    actions: list[str] = []
    if status == "validada":
        if refusal is None and isinstance(found.get("fingerprint"), str):
            actions.append(f'<form method="post" action="{path}/publish">{_hidden("csrf_token", csrf)}'
                           f'{_hidden("fingerprint", found.get("fingerprint"))}'
                           '<button type="submit" class="button primary">Aprobar y usar</button></form>'
                           '<p class="meta">El agente la usa desde la próxima respuesta, en todas las '
                           "conversaciones. No le manda nada a nadie.</p>")
        elif refusal is not None:
            actions.append(f'<div class="notice notice--warn"><p>{_esc(_DONE_WARN.get(refusal, refusal))}</p></div>')
        actions.append(f'<div class="divider"></div><form method="post" action="{path}/discard">'
                       f'{_hidden("csrf_token", csrf)}'
                       f'<input type="text" name="motivo" maxlength="{DISCARD_REASON_MAX}" '
                       'placeholder="Motivo (opcional)" autocomplete="off">'
                       '<button type="submit" class="button">Descartar</button></form>')
    if isinstance(found.get("live"), Mapping):
        actions.append(f'<div class="divider"></div><form method="post" action="{path}/return-to-file">'
                       f'{_hidden("csrf_token", csrf)}'
                       '<input type="text" name="motivo" maxlength="200" placeholder="Motivo (opcional)" '
                       'autocomplete="off"><button type="submit" class="button">Volver al archivo</button></form>'
                       '<p class="meta">El agente deja la versión viva y vuelve al conocimiento firmado del '
                       "repositorio de la instancia.</p>")
    side = (f'<section class="panel panel--side"><div class="panel-heading"><h2>{_esc(label)}</h2>'
            f"<p>{_esc(meaning)}</p></div><div class=\"panel-body\">{''.join(actions)}</div></section>")
    approved = (f'<p class="meta">Aprobada el {_esc(_when(found.get("approved_at"), time_zone))}.</p>'
                if found.get("approved_at") else "")
    touched = ", ".join(_esc(section) for section in sections)
    harness_section = ""
    if harness_enabled:
        rerun = ""
        if harness_rerun and status == "validada":
            rerun = (f'<form method="post" action="{path}/harness">{_hidden("csrf_token", csrf)}'
                     '<button type="submit" class="button">Volver a correr el arnés</button></form>')
        harness_section = _harness_html(harness, rerun=rerun)
    warn_list = f'<ul class="meta">{warn_html}</ul>' if warn_html else ""
    return (f'<header class="page-header"><p class="meta"><a href="{PREFIX}/">Aprobación de flujos</a></p>'
            f'<h1>Conocimiento del agente: versión viva {_esc(found.get("live_version"))}</h1>'
            f'<div class="meta">{_chip(label, chip)}</div></header>'
            f"{_done(done or {})}{serving_html}"
            '<div class="layout"><div class="stack">'
            '<section class="panel"><div class="panel-heading"><h2>Qué cambia y para qué</h2></div>'
            f'<div class="panel-body"><p>{_esc(found.get("purpose"))}</p>'
            f'<p class="meta">Toca: {touched}. Se armó sobre {base_text}.</p>{approved}'
            f"{warn_list}</div></section>"
            '<section class="panel"><div class="panel-heading"><h2>Lo que el agente va a saber distinto</h2></div>'
            f'<div class="panel-body">{_diff_html(preview)}</div></section>'
            f"{harness_section}</div>{side}</div>")


def publish_refusal(found: Mapping[str, Any], source: rules.LiveKnowledgeSource,
                    harness: Any | None = None) -> str | None:
    """Por que el bridge no deja aprobar esta fila hoy, o None. Lo mismo corre al apretar el boton.

    Con ``harness`` (``knowledge_harness.HarnessRunner``), ademas, el arnes tiene que haber dado verde
    para este mismo texto (D-55.11). Va al final: lo demas dice por que no sirve aunque de verde."""
    if found.get("effective_status") != "validada":
        return None
    file = source.file
    if found.get("file_sha256") != file.file_sha256 or found.get("file_version") != file.file_version:
        return "agent_knowledge_file_changed"
    sections = found.get("knowledge")
    try:
        if not isinstance(sections, Mapping):
            raise TypeError("knowledge")
        knowledge = rules.build_knowledge(sections, file_version=file.file_version,
                                          ally_ref=file.knowledge.ally_ref)
    except (KnowledgeError, rules.KnowledgeChangeInvalid, TypeError, ValueError):
        return "agent_knowledge_render_changed"
    if knowledge.rendered_sha256 != found.get("rendered_sha256"):
        return "agent_knowledge_render_changed"
    if rules.instance_blocks(knowledge, context=source.context):
        return "agent_knowledge_rules_failed"
    base = source.edit_base()
    if found.get("base_sha256") != base.sha256 or found.get("base_origin") != base.origin or (
            base.origin == rules.ORIGIN_LIVE and found.get("base_knowledge_id") != base.knowledge_id):
        return "agent_knowledge_base_changed"
    if source.segments_in_force():
        return "knowledge_segments_in_force"
    if harness is not None:
        return harness.refusal(str(found.get("knowledge_id") or ""), found.get("rendered_sha256"))
    return None


def routes(*, source: rules.LiveKnowledgeSource, harness: Any | None = None) -> Callable[[FastAPI, PageTools], None]:
    """Las rutas, para ``register_flow_approval_routes(extra_routes=...)``.

    ``harness`` solo con ``AGENT_LIVE_KNOWLEDGE_HARNESS_ENABLED``; None, las de antes."""

    def register(app: FastAPI, tools: PageTools) -> None:
        store, instance = tools.store, tools.instance

        async def read(session: str, knowledge_id: str) -> Mapping[str, Any]:
            return await store._rpc("approver_get_agent_knowledge_v1", {  # noqa: SLF001 - la misma tienda
                "p_instance": instance, "p_session_hash": session, "p_knowledge_id": knowledge_id})

        def login() -> Response:
            # D-55.9: el login de la pagina no sabe volver a esta seccion; se abre el link otra vez.
            return tools.logged_out(_login(None))

        @app.get(PREFIX + "/k/{knowledge_id}")
        async def knowledge_page(knowledge_id: str, request: Request) -> Response:
            if not _canonical_uuid(knowledge_id):
                return _page("Conocimiento", "No existe esa versión.", 404)
            who = await tools.who(request)
            if who is None:
                return login()
            user, secret = who
            try:
                found = await read(_sha(secret), knowledge_id)
            except SessionInvalid:
                return login()
            except StoreRejected:
                return _page("Conocimiento", "No existe esa versión en esta instancia.", 404)
            except StoreUnavailable:
                logger.info("flow_approval route=knowledge status=503")
                return _page("Conocimiento", "El almacén no contestó. Probá en un rato.", 503)
            logger.info("flow_approval route=knowledge knowledge=%s status=%s", knowledge_id, found.get("status"))
            csrf = tools.config.csrf_token(secret)
            sha = found.get("rendered_sha256") if isinstance(found.get("rendered_sha256"), str) else None
            body = render_knowledge(found, knowledge_id, serving=source.serving_state(knowledge_id),
                                    refusal=publish_refusal(found, source, harness), csrf=csrf,
                                    done=_done_from_query(request, tools.config, secret), time_zone=tools.time_zone,
                                    harness=harness.view(knowledge_id, sha) if harness is not None else None,
                                    harness_enabled=harness is not None,
                                    harness_rerun=harness is not None and harness.can_rerun(knowledge_id, sha))
            return HTMLResponse(_shell("Conocimiento", body, user=user, csrf=csrf, show_user_id=False))

        async def run(request: Request, knowledge_id: str, *, title: str, fields: set[str],
                      optional: frozenset[str], call: Callable[[str, dict[str, str]], Any], ok_code: str) -> Response:
            if not _canonical_uuid(knowledge_id):
                return _page(title, "No existe esa versión.", 404)
            checked = await tools.write_form(request, title=title, fields=fields, optional=optional)
            if isinstance(checked, Response):
                return checked
            _, secret, form = checked
            page = f"{PREFIX}/k/{knowledge_id}"
            try:
                result = await call(_sha(secret), form)
            except SessionInvalid:
                return login()
            except StoreRejected as rejected:
                if rejected.code == "agent_knowledge_not_found":
                    return _page(title, "No existe esa versión en esta instancia.", 404)
                return tools.back(secret, page, hecho=rejected.code)
            except StoreUnavailable:
                return _page(title, "El almacén no contestó. Probá en un rato.", 503)
            code = ok_code if result.get("ok") is True else str(result.get("reason_code") or "agent_knowledge_final")
            logger.info("flow_approval route=knowledge_write knowledge=%s title=%s result=%s", knowledge_id, title,
                        code)
            if result.get("ok") is True:
                # Que se note ya, sin esperar la recarga de los 30 segundos.
                await source.refresh()
            return tools.back(secret, page, hecho=code)

        @app.post(PREFIX + "/k/{knowledge_id}/publish")
        async def knowledge_publish(knowledge_id: str, request: Request) -> Response:
            async def call(session: str, form: dict[str, str]) -> Mapping[str, Any]:
                if not _FINGERPRINT.fullmatch(form["fingerprint"]):
                    return {"ok": False, "reason_code": "agent_knowledge_fingerprint_invalid"}
                found = await read(session, knowledge_id)
                if found.get("effective_status") != "validada":
                    code = "agent_knowledge_expired" if found.get("effective_status") == "vencida" \
                        else "agent_knowledge_not_validated"
                    return {"ok": False, "reason_code": code}
                refusal = publish_refusal(found, source, harness)
                if refusal is not None:
                    return {"ok": False, "reason_code": refusal}
                return await store._rpc("approver_publish_agent_knowledge_v1", {  # noqa: SLF001
                    "p_instance": instance, "p_session_hash": session, "p_knowledge_id": knowledge_id,
                    "p_fingerprint": form["fingerprint"], "p_rendered_sha256": found.get("rendered_sha256")})

            return await run(request, knowledge_id, title="Aprobar y usar", fields={"csrf_token", "fingerprint"},
                             optional=frozenset(), call=call, ok_code="publicada")

        def reason_of(form: dict[str, str], default: str) -> str:
            return form.get("motivo", "").strip() or default

        @app.post(PREFIX + "/k/{knowledge_id}/discard")
        async def knowledge_discard(knowledge_id: str, request: Request) -> Response:
            async def call(session: str, form: dict[str, str]) -> Mapping[str, Any]:
                return await store._rpc("approver_discard_agent_knowledge_v1", {  # noqa: SLF001
                    "p_instance": instance, "p_session_hash": session, "p_knowledge_id": knowledge_id,
                    "p_reason": reason_of(form, DISCARD_DEFAULT_REASON)})

            return await run(request, knowledge_id, title="Descartar", fields={"csrf_token"},
                             optional=frozenset({"motivo"}), call=call, ok_code="descartada")

        @app.post(PREFIX + "/k/{knowledge_id}/return-to-file")
        async def knowledge_return_to_file(knowledge_id: str, request: Request) -> Response:
            async def call(session: str, form: dict[str, str]) -> Mapping[str, Any]:
                return await store._rpc("approver_return_to_file_agent_knowledge_v1", {  # noqa: SLF001
                    "p_instance": instance, "p_session_hash": session,
                    "p_reason": reason_of(form, RETURN_DEFAULT_REASON)})

            return await run(request, knowledge_id, title="Volver al archivo", fields={"csrf_token"},
                             optional=frozenset({"motivo"}), call=call, ok_code="archivo")

        if harness is None:
            return

        @app.post(PREFIX + "/k/{knowledge_id}/harness")
        async def knowledge_harness_rerun(knowledge_id: str, request: Request) -> Response:
            # 5.5 parte 2 (D-55.14): solo una corrida que dio error o se abandono, de una version validada
            # que hoy se podria aprobar (lo vigente es aquello sobre lo que se armo).
            async def call(session: str, form: dict[str, str]) -> Mapping[str, Any]:
                found = await read(session, knowledge_id)
                if found.get("effective_status") != "validada":
                    return {"ok": False, "reason_code": "agent_knowledge_not_validated"}
                before = publish_refusal(found, source)
                if before is not None:
                    return {"ok": False, "reason_code": before}
                sections = found.get("knowledge")
                assert isinstance(sections, Mapping)  # publish_refusal ya lo armo
                draft = rules.build_knowledge(sections, file_version=source.file.file_version,
                                              ally_ref=source.file.knowledge.ally_ref)
                started = harness.start(knowledge_id, base_rendered=source.edit_base().rendered,
                                        draft_rendered=draft.render(), rendered_sha256=draft.rendered_sha256,
                                        rerun=True)
                if started != "corriendo":
                    return {"ok": False, "reason_code": started}
                return {"ok": True}

            return await run(request, knowledge_id, title="Volver a correr el arnés", fields={"csrf_token"},
                             optional=frozenset(), call=call, ok_code="knowledge_harness_started")

    return register
