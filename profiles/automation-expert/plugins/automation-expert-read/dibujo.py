"""El dibujo de los flujos: de la respuesta de /flows (automation-expert-read-v1) al lienzo en HTML.

Arma la fuente del lienzo (el formato de lienzo/armar.py, que es la skill
lienzo-de-flujos del OS copiada tal cual) con lo que contesto el bridge y nada
mas: la definicion `setter-flujo/v1` de cada flujo, su estado y sus numeros
medidos de la ventana de 7 dias. La evidencia de cada flujo es la lectura misma
(el sha del bridge y la hora de la base). Los huecos que declara la lectura van
al resumen del flujo, para que el dibujo no diga mas de lo que se sabe.

No sale a la red ni lee archivos de la instancia: solo traduce y valida. Si el
validador del lienzo corta (un telefono, un mail o una clave en un texto, un
nodo vigente sin evidencia), no hay dibujo.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

_armar = import_module(f"{__package__}.lienzo.armar") if __package__ else import_module("lienzo.armar")

_ESTADOS = {"on": "vigente", "off": "sin_activar", "paused": "espera"}
_NOMBRES_DE_ESTADO = {"on": "prendido", "off": "apagado", "paused": "pausado"}
_LETRAS = {"carrito": "A", "pago_fallido": "B", "precheckout": "P", "descuento": "D", "reactivacion": "R"}


def _texto(valor: object) -> str:
    return valor if isinstance(valor, str) else ""


def _metrica(observed: object) -> str | None:
    if not isinstance(observed, dict):
        return None
    dias = observed.get("window_days")
    if not isinstance(dias, int):
        return None
    partes: list[str] = []
    for clave, nombre in (("triggers", "disparos"), ("starts", "arranques"),
                          ("accepted_by_chatwoot", "aceptados por Chatwoot"),
                          ("sent", "enviados"), ("failed", "fallidos")):
        valor = observed.get(clave)
        if isinstance(valor, int) and not isinstance(valor, bool):
            partes.append(f"{valor} {nombre}")
    return f"{', '.join(partes)} en {dias} días" if partes else None


def _flujo(flow: dict[str, Any], huecos: list[str], evidencia: list[dict[str, str]]) -> dict[str, Any]:
    clave = _texto(flow.get("flow_key"))
    status = flow.get("status") if isinstance(flow.get("status"), dict) else {}
    state = _texto(status.get("state"))
    definicion = flow.get("definition") if isinstance(flow.get("definition"), dict) else None
    nombre = _texto((definicion or {}).get("nombre")) or clave
    resumen = [f"Hoy está {_NOMBRES_DE_ESTADO.get(state, 'en un estado que la lectura no reconoce')}"]
    since = _texto(status.get("since"))
    if since:
        resumen[0] += f" desde el {since[:10]}"
    version = flow.get("version")
    if isinstance(version, dict) and version:
        resumen.append(" · ".join(_texto(v) for v in version.values() if _texto(v)))
    resumen += huecos
    hoy: dict[str, Any] = {
        "estado": _ESTADOS.get(state, "espera"),
        "evidencia": evidencia,
    }
    if definicion and (definicion.get("pasos") or definicion.get("dispara")):
        hoy["definicion"] = definicion
        metrica = _metrica(flow.get("observed"))
        if metrica:
            hoy["nodos_extra"] = {"dispara": {"metrica": metrica}}
    if not (definicion and definicion.get("pasos")):
        hoy["vacio"] = "La lectura no trae pasos para este flujo: hoy no le escribe a nadie desde acá."
    return {
        "id": clave,
        "letra": _LETRAS.get(clave, clave[:1].upper()),
        "nombre": nombre,
        "resumen": ". ".join(r for r in resumen if r) + ".",
        "versiones": {"hoy": hoy},
    }


def fuente_desde_lectura(payload: dict[str, Any], primero: str | None = None) -> dict[str, Any]:
    """La fuente del lienzo con lo que devolvio GET /flows. `primero` pone ese flujo adelante."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    flows = [f for f in (data.get("flows") or []) if isinstance(f, dict)]
    instancia = payload.get("instance") if isinstance(payload.get("instance"), dict) else {}
    nombre_instancia = _texto(instancia.get("ally_ref")) or _texto(instancia.get("funnel_ref")) or "instancia"
    leido = _texto(payload.get("database_at")) or _texto(payload.get("generated_at"))
    sha = _texto(payload.get("bridge_git_sha"))[:12]
    evidencia = [{
        "ref": f"bridge@{sha or 'sin-sha'}:/internal/automation-expert/v1/flows",
        "nota": f"leído de la base a las {leido or 'hora desconocida'}",
    }]
    huecos_por_flujo: dict[str, list[str]] = {}
    for gap in payload.get("gaps") or []:
        if not isinstance(gap, dict):
            continue
        scope = _texto(gap.get("scope"))
        texto = f"No se sabe {_texto(gap.get('what'))}: {_texto(gap.get('why'))}".strip(": ")
        if scope.startswith("flow:"):
            huecos_por_flujo.setdefault(scope[5:], []).append(texto)
    if primero:
        flows.sort(key=lambda f: f.get("flow_key") != primero)
    return {
        "instancia": nombre_instancia,
        "titulo": f"Los flujos de {nombre_instancia.upper()}, como corren hoy",
        "titulo_pagina": f"Flujos de {nombre_instancia.upper()}",
        "eyebrow": f"Automation Expert · {nombre_instancia.upper()} · {_texto(instancia.get('product_version'))}",
        "medido_el": leido or "sin hora",
        "generado_el": _texto(payload.get("generated_at")),
        "vistas": ["hoy"],
        "nombres_vistas": {"hoy": "Hoy"},
        "fuentes": [{"ref": evidencia[0]["ref"], "nota": "la lectura del Automation Expert"}],
        "flujos": [_flujo(f, huecos_por_flujo.get(_texto(f.get("flow_key")), []), evidencia) for f in flows],
    }


def armar_dibujo(payload: dict[str, Any], primero: str | None = None) -> tuple[str | None, list[str], list[str]]:
    """El HTML del lienzo, los avisos y los errores. Sin HTML si hay errores."""
    datos = _armar.construir(fuente_desde_lectura(payload, primero), {})
    errores, avisos = _armar.validar(datos)
    if errores:
        return None, avisos, errores
    return _PAGINA.format(cuerpo=_armar.armar_html(datos)), avisos, []


# lienzo.html es el cuerpo de un artifact: el esqueleto lo ponia la publicacion. En
# Slack el archivo se abre solo en el navegador, asi que se lo damos completo.
_PAGINA = (
    "<!doctype html>\n<html lang=\"es\">\n<head>\n<meta charset=\"utf-8\">\n"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n</head>\n"
    "<body>\n{cuerpo}\n</body>\n</html>\n"
)


def guardar(html: str, carpeta: Path, nombre: str) -> Path:
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / nombre
    tmp = ruta.with_name(ruta.name + ".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(ruta)
    return ruta
