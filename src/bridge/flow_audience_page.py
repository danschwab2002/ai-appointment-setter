"""La pagina de los envios a una audiencia (E-484, P20; D7): ver cuantos van a salir y confirmar.

Una seccion de la pagina de aprobacion (``register_flow_approval_routes(extra_routes=...)``),
con la misma sesion de «Entrar con Slack» y la misma lista de aprobadores. Solo existe
con ``FLOW_AUDIENCE_TRIGGERS_ENABLED`` (apagado, las rutas dan 404).

* ``GET  /flow-approval/envios``: los flujos ``manual`` que corren, con «Armar la
  lista», y las ocurrencias de la instancia: cuantas personas van a salir, cuantas
  quedaron afuera y por que, el ritmo y como va el envio. Sin telefonos ni nombres.
* ``POST /flow-approval/envios/armar``: arma la lista de un flujo ``manual`` (la arma
  el bridge, con quien la pidio). Queda esperando; nadie la recibe todavia.
* ``POST /flow-approval/envios/{id}/confirmar``: **el si de una persona**. Hay que
  escribir el numero exacto de personas que van a salir y marcar «Confirmo»: un
  numero distinto o sin la marca no confirma (lo decide la base, con la sesion).
* ``POST /flow-approval/envios/{id}/frenar``: frenar una ocurrencia que todavia no
  termino. Una frenada no sale nunca mas.

El AE nunca confirma ni dispara: arma el flujo y manda el link de esta pagina.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from .flow_approval import (
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
from .flow_audience_sweeper import AudienceSweeper

__all__ = ["PATH", "render_audience", "routes"]

logger = logging.getLogger(__name__)

PATH = PREFIX + "/envios"
TITLE = "Envíos a una audiencia"

_STATE = {
    "materializada": ("Esperando tu sí", "chip--info"),
    "confirmada": ("Confirmado, por salir", "chip--ok"),
    "enviando": ("Saliendo", "chip--ok"),
    "terminada": ("Terminado", None),
    "frenada": ("Frenado", "chip--warn"),
}
_FORM = {"fecha": "En una fecha", "cada": "Cada cierto tiempo", "manual": "Cuando lo lanza una persona"}
_OMIT = {"baja": "pidieron no recibir más", "sin_permiso": "sin permiso vigente", "fuera_de_cohorte":
         "fuera del grupo de prueba", "repetida": "ya lo recibieron hace poco"}
_BRAKE = {"meta_salud:red": "la salud del número está en rojo", "meta_salud:yellow":
          "la salud del número está en amarillo", "meta_salud:sin_lectura": "no se pudo leer la salud del número",
          "meta_salud:vieja": "la lectura de la salud del número tiene más de 26 horas",
          "meta_salud:blocked": "Meta dice que el número no puede enviar",
          "meta_salud:limited": "Meta limitó los envíos del número",
          "meta_salud:limite_bajo": "el límite de mensajes del número es bajo (2.000 por día o menos)",
          "a_mano": "lo frenó una persona"}
_DONE = {
    "armada": "Lista armada. Revisá cuántas personas van a salir y confirmá abajo.",
    "confirmada": "Confirmado. Sale al ritmo por hora del flujo, y se frena solo si baja la salud del número.",
    "frenada": "Frenado. Esa lista no sale más.",
    "count_mismatch": "El número que escribiste no es el de la lista. No se confirmó.",
    "second_step_missing": "Falta marcar «Confirmo». No se confirmó.",
    "not_waiting_for_confirmation": "Esa lista ya no espera confirmación.",
    "not_brakeable": "Esa lista ya terminó o ya estaba frenada.",
    "flow_not_running": "El flujo ya no corre (se pausó, se apagó o cambió de versión).",
    "flow_not_manual": "Ese flujo no se lanza a mano.",
}


def _done(done: Mapping[str, str]) -> str:
    code = done.get("hecho")
    if not code:
        return ""
    text = _DONE.get(code, "No se pudo completar. Probá de nuevo o avisá al equipo.")
    return f'<div class="notice"><p>{_esc(text)}</p></div>'


def _omitted_text(by_reason: object) -> str:
    if not isinstance(by_reason, Mapping) or not by_reason:
        return ""
    parts = [f"{int(n)} {_OMIT.get(str(reason), str(reason))}" for reason, n in sorted(by_reason.items())
             if isinstance(n, int)]
    return f"<p>Quedaron afuera: {_esc(', '.join(parts))}.</p>" if parts else ""


def _occurrence_html(row: Mapping[str, Any], *, csrf: str, time_zone: str | None) -> str:
    occurrence_id = str(row.get("occurrence_id") or "")
    if not _canonical_uuid(occurrence_id):
        return ""
    state = str(row.get("state") or "")
    label, chip = _STATE.get(state, (state, None))
    members = int(row.get("member_count") or 0)
    per_hour = int(row.get("per_hour") or 0)
    lines = [
        f"<p><strong>{_esc(row.get('flow_key'))}</strong> · {_esc(_FORM.get(str(row.get('form')), ''))} · "
        f"{_chip(label, chip)}</p>",
        f'<p class="count">Van a salir: <strong>{members}</strong> personas, a {per_hour} por hora como máximo.</p>',
        _omitted_text(row.get("omitted_by_reason")),
    ]
    if state in ("enviando", "terminada"):
        lines.append(f"<p>Salieron {int(row.get('released_count') or 0)}; el motor abrió el envío a "
                     f"{int(row.get('started_count') or 0)}.</p>")
    if state == "frenada":
        lines.append(f"<p>Frenado: {_esc(_BRAKE.get(str(row.get('brake_reason')), str(row.get('brake_reason'))))}.</p>")
    lines.append(f"<p class=\"meta\">Armada {_esc(_when(row.get('materialized_at'), time_zone))}</p>")
    if state == "materializada":
        lines.append(
            f'<form method="post" action="{PATH}/{occurrence_id}/confirmar" class="confirm">'
            f'{_hidden("csrf_token", csrf)}'
            f'<label>Para confirmar, escribí cuántas personas van a salir ({members}):'
            f' <input type="number" name="conteo" min="0" max="5000" required inputmode="numeric"></label>'
            '<label><input type="checkbox" name="segundo_paso" value="si"> Confirmo que este mensaje sale a esas '
            "personas, al ritmo por hora del flujo.</label>"
            '<button type="submit" class="primary">Confirmar el envío</button></form>')
    if state in ("materializada", "confirmada", "enviando"):
        lines.append(f'<form method="post" action="{PATH}/{occurrence_id}/frenar">{_hidden("csrf_token", csrf)}'
                     '<button type="submit">Frenar</button></form>')
    return f'<section class="panel"><div class="panel-body">{"".join(lines)}</div></section>'


def render_audience(read: Mapping[str, Any], *, csrf: str, done: Mapping[str, str], time_zone: str | None) -> str:
    """La pagina: los flujos manuales con su boton y las ocurrencias. Sin telefonos."""
    flows = [f for f in read.get("flows") or [] if isinstance(f, Mapping)]
    manual = [f for f in flows if f.get("form") == "manual" and _canonical_uuid(str(f.get("flow_id") or ""))]
    flow_html = "".join(
        f'<li><strong>{_esc(f.get("nombre") or f.get("flow_key"))}</strong> ({_esc(f.get("flow_key"))})'
        f'<form method="post" action="{PATH}/armar">{_hidden("csrf_token", csrf)}{_hidden("flujo", f["flow_id"])}'
        '<button type="submit">Armar la lista</button></form></li>' for f in manual)
    occurrences = "".join(_occurrence_html(row, csrf=csrf, time_zone=time_zone)
                          for row in read.get("occurrences") or [] if isinstance(row, Mapping))
    return (f'<header class="page-header"><p class="meta"><a href="{PREFIX}/">Aprobación de flujos</a></p>'
            f"<h1>{_esc(TITLE)}</h1></header>{_done(done)}"
            '<section class="panel"><div class="panel-heading"><h2>Lanzar un envío</h2>'
            "<p>Armar la lista no manda nada: después ves cuántas personas van a salir y confirmás.</p></div>"
            f'<div class="panel-body">{f"<ul>{flow_html}</ul>" if manual else "<p>No hay flujos para lanzar a mano.</p>"}'
            "</div></section>"
            '<div class="stack"><h2>Envíos</h2>'
            f'{occurrences or "<p>Todavía no hay envíos.</p>"}</div>')


def routes(*, sweeper: AudienceSweeper) -> Callable[[FastAPI, PageTools], None]:
    """Las rutas, para ``register_flow_approval_routes(extra_routes=...)``."""

    def register(app: FastAPI, tools: PageTools) -> None:
        store, instance = tools.store, tools.instance

        async def read(session_hash: str) -> dict[str, Any]:
            return await store._rpc("approver_read_flow_audience_v1",  # noqa: SLF001 - la misma tienda de la pagina
                                    {"p_instance": instance, "p_session_hash": session_hash})

        @app.get(PATH)
        async def audience_page(request: Request) -> Response:
            who = await tools.who(request)
            if who is None:
                return tools.logged_out(_login(None))
            user, secret = who
            try:
                found = await read(_sha(secret))
            except SessionInvalid:
                return tools.logged_out(_login(None))
            except (StoreRejected, StoreUnavailable):
                logger.info("flow_approval route=audience status=503")
                return _page(TITLE, "El almacén no contestó. Probá en un rato.", 503)
            csrf = tools.config.csrf_token(secret)
            body = render_audience(found, csrf=csrf, done=_done_from_query(request, tools.config, secret),
                                   time_zone=tools.time_zone)
            return HTMLResponse(_shell(TITLE, body, user=user, csrf=csrf, show_user_id=False))

        @app.post(PATH + "/armar")
        async def audience_build(request: Request) -> Response:
            checked = await tools.write_form(request, title="Armar la lista", fields={"csrf_token", "flujo"})
            if isinstance(checked, Response):
                return checked
            user, secret, form = checked
            if not _canonical_uuid(form["flujo"]):
                return _page("Armar la lista", "No existe ese flujo.", 404)
            try:
                result = await sweeper.materialize_manual(form["flujo"], slack_user_id=user)
            except Exception as error:  # noqa: BLE001 - la pagina nunca muestra una traza
                logger.warning("flow_approval route=audience_build error=%s", type(error).__name__)
                return _page("Armar la lista", "No se pudo armar la lista. Probá en un rato.", 503)
            code = "armada" if result.ok else (result.reason_code or "flow_audience_final")
            logger.info("flow_approval route=audience_build flow=%s result=%s members=%s", form["flujo"], code,
                        result.member_count)
            return tools.back(secret, PATH, hecho=code.split(":")[0])

        async def write(request: Request, occurrence_id: str, *, title: str, fields: set[str],
                        optional: frozenset[str], call: Callable[[str, dict[str, str]], Any], ok_code: str) -> Response:
            if not _canonical_uuid(occurrence_id):
                return _page(title, "No existe ese envío.", 404)
            checked = await tools.write_form(request, title=title, fields=fields, optional=optional)
            if isinstance(checked, Response):
                return checked
            _, secret, form = checked
            try:
                result = await call(_sha(secret), form)
            except SessionInvalid:
                return tools.logged_out(_login(None))
            except StoreRejected as rejected:
                if rejected.code == "flow_audience_occurrence_not_found":
                    return _page(title, "No existe ese envío en esta instancia.", 404)
                return tools.back(secret, PATH, hecho=rejected.code)
            except StoreUnavailable:
                return _page(title, "El almacén no contestó. Probá en un rato.", 503)
            code = ok_code if result.get("ok") is True else str(result.get("reason_code") or "flow_audience_final")
            logger.info("flow_approval route=audience_write occurrence=%s title=%s result=%s", occurrence_id, title,
                        code)
            return tools.back(secret, PATH, hecho=code)

        @app.post(PATH + "/{occurrence_id}/confirmar")
        async def audience_confirm(occurrence_id: str, request: Request) -> Response:
            async def call(session: str, form: dict[str, str]) -> Mapping[str, Any]:
                raw = form["conteo"].strip()
                if not raw.isdigit() or len(raw) > 5:
                    return {"ok": False, "reason_code": "count_mismatch"}
                return await store._rpc("approver_confirm_flow_audience_v1", {  # noqa: SLF001
                    "p_instance": instance, "p_session_hash": session, "p_occurrence_id": occurrence_id,
                    "p_expected_count": int(raw), "p_second_step": form.get("segundo_paso") == "si"})

            return await write(request, occurrence_id, title="Confirmar el envío", fields={"csrf_token", "conteo"},
                               optional=frozenset({"segundo_paso"}), call=call, ok_code="confirmada")

        @app.post(PATH + "/{occurrence_id}/frenar")
        async def audience_brake(occurrence_id: str, request: Request) -> Response:
            async def call(session: str, _: dict[str, str]) -> Mapping[str, Any]:
                return await store._rpc("approver_brake_flow_audience_v1", {  # noqa: SLF001
                    "p_instance": instance, "p_session_hash": session, "p_occurrence_id": occurrence_id})

            return await write(request, occurrence_id, title="Frenar", fields={"csrf_token"},
                               optional=frozenset(), call=call, ok_code="frenada")

    return register
