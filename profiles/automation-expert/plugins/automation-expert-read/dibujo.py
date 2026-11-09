"""El dibujo de los flujos: de la respuesta de /flows (automation-expert-read-v1) al lienzo en HTML.

Arma la fuente del lienzo (el formato de lienzo/armar.py, que es la skill
lienzo-de-flujos del OS copiada tal cual, y por eso no se toca) con lo que
contesto el bridge y nada mas: la definicion `setter-flujo/v1` de cada flujo, su
estado y sus numeros medidos de la ventana de 7 dias. Con `flow_key`, solo ese
flujo.

El dibujo se le pasa a un equipo que no conoce el sistema (E-367): lo que armar.py
escribe para quien lo opera (codigos de Hotmart, nombres de plantilla, bolsa, claves
de flujo) se traduce aca, despues de armar, a las palabras de la tabla del SOUL.
Lo que no tiene traduccion util para un dueno no se muestra. Los huecos de un flujo
quedan en una sola frase; el detalle lo devuelve la herramienta en `gaps`.

No sale a la red ni lee archivos de la instancia: solo traduce y valida. Si el
validador del lienzo corta (un telefono, un mail o una clave en un texto, un
nodo vigente sin evidencia), no hay dibujo.
"""

from __future__ import annotations

import copy
from datetime import datetime
from importlib import import_module
import json
from pathlib import Path
import re
from typing import Any

_armar = import_module(f"{__package__}.lienzo.armar") if __package__ else import_module("lienzo.armar")

_ESTADOS = {"on": "vigente", "off": "sin_activar", "paused": "espera", "cohort": "sin_activar"}
_NOMBRES_DE_ESTADO = {"on": "prendido", "off": "apagado", "paused": "pausado", "cohort": "en el grupo de prueba"}
_LETRAS = {"carrito": "A", "pago_fallido": "B", "precheckout": "P", "descuento": "D", "reactivacion": "R"}
_ORIGEN_DEL_DISPARO = {
    "carrito": "Lo avisa Hotmart",
    "pago_fallido": "Lo avisa Hotmart",
    "ficha_impresa": "Lo avisa Hotmart",
    "ficha_vencida": "Lo avisa Hotmart",
    "compra": "Lo avisa Hotmart",
    "intencion": "Dejó sus datos en la página y aceptó que le escriban",
}
_UNA_POR = {"persona": "persona", "persona_y_oferta": "persona y producto", "conversacion": "conversación"}
_CONDICIONES = {
    ("tipo_de_rechazo", "sin_saldo"): "¿Fue por falta de saldo?",
    ("tipo_de_rechazo", "banco"): "¿Lo rechazó el banco?",
    ("rechazos", "uno"): "¿Es su primer rechazo?",
    ("rechazos", "varios"): "¿Le rechazaron más de una vez?",
    ("descuento_previo", "si"): "¿Ya recibió un descuento antes?",
    ("descuento_previo", "no"): "¿Todavía no recibió un descuento?",
}
_HUECOS = "Este dibujo puede no mostrar todos los detalles de este flujo"
_DATOS_DEL_SISTEMA = "Datos del sistema"


def _ciudad(zona: str) -> str:
    return {"Mexico_City": "Ciudad de México"}.get(zona, zona.replace("_", " "))


def _texto(valor: object) -> str:
    return valor if isinstance(valor, str) else ""


def _dia(iso: str) -> str:
    """Del ISO de la lectura a dd/mm/aaaa."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d/%m/%Y")
    except ValueError:
        return iso[:10]


def _hora(iso: str) -> str:
    """Del ISO de la lectura a dd/mm/aaaa hh:mm UTC. Lo que no se entiende, se devuelve tal cual."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d/%m/%Y %H:%M UTC")
    except ValueError:
        return iso


def _nombre_de_clave(clave: str) -> str:
    """Una clave de flujo sin nombre propio, como se la leeria en voz alta."""
    palabras = [p for p in clave.split("_") if p not in ("nativo", "legado", "motor")]
    return " ".join(palabras or [clave]).capitalize()


def _metrica(observed: object) -> str | None:
    if not isinstance(observed, dict):
        return None
    dias = observed.get("window_days")
    if not isinstance(dias, int):
        return None
    partes: list[str] = []
    envios = "accepted_by_chatwoot" if isinstance(observed.get("accepted_by_chatwoot"), int) else "sent"
    for clave, nombre in (("triggers", "avisos"), ("starts", "entraron al flujo"),
                          (envios, "mensajes enviados"), ("failed", "no se pudieron enviar")):
        valor = observed.get(clave)
        if isinstance(valor, int) and not isinstance(valor, bool):
            partes.append(f"{valor} {nombre}")
    return f"{', '.join(partes)} en los últimos {dias} días" if partes else None


def _sub_del_disparo(definicion: dict[str, Any]) -> str:
    dispara = definicion.get("dispara") if isinstance(definicion.get("dispara"), dict) else {}
    if "evento" in dispara:
        return _ORIGEN_DEL_DISPARO.get(_texto(dispara.get("evento")), "")
    silencio = dispara.get("silencio") if isinstance(dispara.get("silencio"), dict) else {}
    return f"Sin respuesta entre {_armar.dur(silencio.get('desde'))} y {_armar.dur(silencio.get('hasta'))}"


def _detalle_del_fin(definicion: dict[str, Any]) -> str:
    termina = definicion.get("termina") if isinstance(definicion.get("termina"), dict) else {}
    cortes = [_armar.SALIDAS.get(x, x) for x in termina.get("si") or []] + _armar.CORTES_DEL_CODIGO
    return "Se corta antes de terminar si pasa algo de esto: " + "; ".join(cortes) + "."


def _sub_del_fin(definicion: dict[str, Any]) -> str | None:
    """Un `vence` en null es que la lectura no lo sabe (legacy_scope_runtime_only), no que no tenga plazo."""
    termina = definicion.get("termina") if isinstance(definicion.get("termina"), dict) else {}
    if "vence" not in termina or termina["vence"] is not None:
        return None
    cortes = [_armar.SALIDAS.get(x, x) for x in termina.get("si") or []] + ["baja"]
    return "plazo sin confirmar · corta si " + ", ".join(cortes)


def _flujo(flow: dict[str, Any], con_huecos: bool, evidencia: list[dict[str, str]]) -> dict[str, Any]:
    clave = _texto(flow.get("flow_key"))
    status = flow.get("status") if isinstance(flow.get("status"), dict) else {}
    state = _texto(status.get("state"))
    definicion = flow.get("definition") if isinstance(flow.get("definition"), dict) else None
    nombre = _texto((definicion or {}).get("nombre")) or _nombre_de_clave(clave)
    resumen = [f"Hoy está {_NOMBRES_DE_ESTADO.get(state, 'en un estado que no se puede confirmar')}"]
    since = _texto(status.get("since"))
    if since:
        resumen[0] += f" desde el {_dia(since)}"
    if con_huecos:
        resumen.append(_HUECOS)
    hoy: dict[str, Any] = {
        "estado": _ESTADOS.get(state, "espera"),
        "evidencia": evidencia,
    }
    if definicion and (definicion.get("pasos") or definicion.get("dispara")):
        hoy["definicion"] = definicion
        extra: dict[str, Any] = {"sub": _sub_del_disparo(definicion)}
        metrica = _metrica(flow.get("observed"))
        if metrica:
            extra["metrica"] = metrica
        fin: dict[str, Any] = {"detalle": _detalle_del_fin(definicion)}
        sub_del_fin = _sub_del_fin(definicion)
        if sub_del_fin:
            fin["sub"] = sub_del_fin
        hoy["nodos_extra"] = {"dispara": extra, "fin": fin}
    if definicion is None:
        hoy["vacio"] = "El detalle de este flujo no se pudo leer."
    elif not definicion.get("pasos"):
        hoy["vacio"] = "Este flujo no tiene pasos: hoy no le escribe a nadie."
    return {
        "id": clave,
        "letra": _LETRAS.get(clave, clave[:1].upper()),
        "nombre": nombre,
        "resumen": ". ".join(r for r in resumen if r) + ".",
        "versiones": {"hoy": hoy},
    }


def _nombres_de_flujos(flows: list[dict[str, Any]]) -> dict[str, str]:
    nombres: dict[str, str] = {}
    for flow in flows:
        clave = _texto(flow.get("flow_key"))
        definicion = flow.get("definition") if isinstance(flow.get("definition"), dict) else {}
        nombres[clave] = _texto(definicion.get("nombre")) or _nombre_de_clave(clave)
    return nombres


def fuente_desde_lectura(payload: dict[str, Any], solo: str | None = None) -> dict[str, Any]:
    """La fuente del lienzo con lo que devolvio GET /flows. Con `solo`, unicamente ese flujo."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    flows = [f for f in (data.get("flows") or []) if isinstance(f, dict)]
    instancia = payload.get("instance") if isinstance(payload.get("instance"), dict) else {}
    nombre_instancia = _texto(instancia.get("ally_ref")) or _texto(instancia.get("funnel_ref")) or "instancia"
    leido = _texto(payload.get("database_at")) or _texto(payload.get("generated_at"))
    medido = _hora(leido) if leido else "sin hora"
    evidencia = [{"ref": _DATOS_DEL_SISTEMA, "nota": f"leídos el {medido}"}]
    con_huecos = {
        _texto(gap.get("scope"))[5:]
        for gap in payload.get("gaps") or []
        if isinstance(gap, dict) and _texto(gap.get("scope")).startswith("flow:")
    }
    if solo:
        flows = [f for f in flows if f.get("flow_key") == solo]
    nombres = _nombres_de_flujos(flows)
    titulo = f"Los flujos de {nombre_instancia.upper()}, como corren hoy"
    if solo and flows:
        titulo = f"{nombres[solo]}: cómo funciona hoy"
    generado = _texto(payload.get("generated_at"))
    return {
        "instancia": nombre_instancia,
        "titulo": titulo,
        "titulo_pagina": titulo,
        "eyebrow": f"Cómo funciona · {nombre_instancia.upper()}",
        "medido_el": medido,
        "generado_el": _hora(generado) if generado else "",
        "vistas": ["hoy"],
        "nombres_vistas": {"hoy": "Hoy"},
        "fuentes": [{"ref": _DATOS_DEL_SISTEMA, "nota": "lo que el sistema tiene guardado de cada flujo"}],
        "flujos": [_flujo(f, _texto(f.get("flow_key")) in con_huecos, evidencia) for f in flows],
    }


def _lista_de_flujos(valor: object, nombres: dict[str, str]) -> str:
    return ", ".join(nombres.get(c, _nombre_de_clave(c)) for c in _texto(valor).split(", ") if c)


def _dato_en_negocio(fila: list[Any], nombres: dict[str, str]) -> list[Any]:
    etiqueta, valor = fila[0], _texto(fila[1])
    if etiqueta == "Topes":
        topes = re.sub(r" · bolsa .*$", "", valor)  # sin leer viene None: armar.py lo escribe tal cual
        return [etiqueta, "sin confirmar" if "None" in topes else topes]
    if etiqueta == "Contestó a":
        if valor == "cualquier plantilla del recuperador":
            return [etiqueta, "cualquier mensaje automático"]
        return [etiqueta, _lista_de_flujos(valor, nombres)]
    if etiqueta in ("Le gana a", "No arranca mientras"):
        return [etiqueta, _lista_de_flujos(valor, nombres)]
    if etiqueta == "Horario":
        return [etiqueta, re.sub(r" \([^)]*/([^)]*)\)$", lambda m: f" (hora de {_ciudad(m.group(1))})", valor)]
    if etiqueta == "Una vez por":
        una_por, _, resto = valor.partition(" · ")
        una_por = _UNA_POR.get(una_por.replace(" ", "_"), una_por)
        resto = "repetición sin confirmar" if resto == "puede repetirse a None" else resto
        return [etiqueta, f"{una_por} · {resto}" if resto else una_por]
    return fila


def _condicion_en_negocio(nodo: dict[str, Any]) -> None:
    nodo.pop("condicion", None)
    titulo = _texto(nodo.get("titulo"))
    if titulo.startswith("¿La ventana"):
        nodo["titulo"] = "¿Escribió en las últimas 24 horas?"
        return
    partes = re.fullmatch(r"¿(\w+) es (.+)\?", titulo)
    if partes:
        nodo["titulo"] = _CONDICIONES.get((partes.group(1), partes.group(2)), "¿Se cumple esta condición?")
    elif re.search(r"\b[A-Z_]{4,}\b", titulo.replace("OXXO", "")):  # un codigo de Hotmart sin traduccion
        nodo["titulo"] = "¿Se cumple esta condición?"


def _paso_raro_en_negocio(nodo: dict[str, Any], version: dict[str, Any]) -> None:
    """Un paso que armar.py no sabe dibujar llega como nota con el JSON del paso: el JSON no se muestra."""
    try:
        paso = json.loads(_texto(nodo.get("detalle")))
    except ValueError:
        paso = None
    etiqueta = paso.get("etiquetar") if isinstance(paso, dict) else None
    if isinstance(etiqueta, str):
        nodo.update(
            tipo="accion", estado=version.get("estado"), titulo=f"Le pone la etiqueta «{etiqueta}»",
            evidencia=copy.deepcopy(version.get("evidencia") or []),
        )
        nodo.pop("detalle", None)
        return
    nodo.update(titulo="Paso que no se puede mostrar", detalle="Este paso existe, pero este dibujo no sabe mostrarlo.")


def _plantilla_en_negocio(nodo: dict[str, Any], orden: int | None) -> None:
    """El nombre interno de la plantilla no se muestra; sin catalogo, tampoco hay texto para mostrar."""
    plantilla = nodo.get("plantilla") or {}
    nodo["titulo"] = f"Mensaje {orden} por WhatsApp" if orden else "Mensaje por WhatsApp"
    plantilla.pop("nombre", None)
    plantilla.pop("idioma", None)
    if _texto(plantilla.get("texto")).startswith("(texto de "):
        plantilla["texto"] = "(El texto de este mensaje no viene en este dibujo)"
        nodo["supuesto"] = "El texto no está en este dibujo: se ve en la lista de plantillas."
    pendiente = nodo.get("pendiente")
    if isinstance(pendiente, dict) and _texto(pendiente.get("texto")).startswith("La plantilla"):
        pendiente["texto"] = "Este mensaje todavía no existe: hay que crearlo y esperar la aprobación de Meta."


def _en_negocio(datos: dict[str, Any], nombres: dict[str, str]) -> None:
    """Pasa lo que armar.py escribio para quien opera a las palabras de quien usa el dibujo."""
    for flujo in datos.get("flujos") or []:
        for version in (flujo.get("versiones") or {}).values():
            nodos = version.get("nodos") or []
            mensajes = sum(1 for n in nodos if n.get("tipo") == "plantilla")
            orden = 0
            for nodo in nodos:
                tipo = nodo.get("tipo")
                if tipo == "plantilla":
                    orden += 1
                    _plantilla_en_negocio(nodo, orden if mensajes > 1 else None)
                elif tipo == "condicion":
                    _condicion_en_negocio(nodo)
                elif tipo == "nota":
                    _paso_raro_en_negocio(nodo, version)
                elif tipo == "espera" and isinstance(nodo.get("pendiente"), dict):
                    nodo["pendiente"]["texto"] = "Falta definir cuánto espera."
                elif tipo == "fin" and _texto(nodo.get("sub")).startswith("rama "):
                    nodo.pop("sub")
                if nodo.get("datos"):
                    nodo["datos"] = [_dato_en_negocio(fila, nombres) for fila in nodo["datos"]]


def armar_dibujo(payload: dict[str, Any], solo: str | None = None) -> tuple[str | None, list[str], list[str]]:
    """El HTML del lienzo, los avisos y los errores. Sin HTML si hay errores."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    nombres = _nombres_de_flujos([f for f in (data.get("flows") or []) if isinstance(f, dict)])
    datos = _armar.construir(fuente_desde_lectura(payload, solo), {})
    _en_negocio(datos, nombres)
    errores, avisos = _armar.validar(datos)
    if errores:
        return None, avisos, errores
    datos.pop("fuentes", None)  # ya validadas; en la pagina solo serian un «1 fuentes citadas»
    cuerpo = _armar.armar_html(datos)
    for de, a in _TEXTOS_DE_LA_PAGINA:
        cuerpo = cuerpo.replace(de, a)
    return _PAGINA.format(cuerpo=cuerpo), avisos, []


# lienzo.html es copia tal cual de la skill, asi que lo que no le sirve a este dibujo se cambia en la salida:
# sin `reglas` su panel quedaria con el titulo solo (la capa sigue, la pide el script), y la ayuda va sin voseo.
_REGLAS = '<div class="solo-ancho">\n        <h2>Reglas de todos los flujos</h2>'
_TEXTOS_DE_LA_PAGINA = (
    (_REGLAS, _REGLAS.replace('class="solo-ancho"', 'class="solo-ancho" style="display:none"')),
    ("Arrastrá para recorrer", "Arrastra para recorrer"),
    ("tocá un paso", "toca un paso"),
)

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
