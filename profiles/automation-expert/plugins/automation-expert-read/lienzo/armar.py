#!/usr/bin/env python3
"""Arma el lienzo de flujos de una instancia del recuperador.

Lee la FUENTE de una instancia (formato en esquema.md): por cada flujo y cada
versión, una definición `setter-flujo/v1` (la del producto) o nodos dibujados a
mano. Traduce las definiciones a nodos y flechas, completa los textos de las
plantillas desde el catálogo de Chatwoot, marca qué cambia contra «hoy», valida
y lo inyecta en lienzo.html. La página resultante es la que se publica.

    python3 armar.py <fuente.json> <salida.html> [--catalogo cat.json ...] [--definiciones-hoy defs.json] [--simulacion sim.json] [--lienzo-json out.json]
    python3 armar.py <fuente.json> --solo-validar [--catalogo cat.json ...]
    python3 armar.py --selftest

--definiciones-hoy reemplaza la `definicion` de la versión «hoy» de cada flujo por la
que trae el archivo (el fixture del producto, `{"definitions": [...]}`, o la lista que
devuelve la consulta al almacén), dice qué cambió y reescribe la fuente.

--simulacion pone en cada nodo de plantilla cuántas personas habrían recibido ese
mensaje en la semana de la captura, según `flow_engine_simulate.py --lienzo` del
producto (`setter-simulacion-lienzo/v1`). Va en `simulado`, nunca en `metrica`: es
una predicción, no una medición. La propuesta que se simula tiene que ser la
`definicion` de la misma versión del lienzo; si un paso simulado no cae en un nodo
de plantilla, no se arma.

Sale con 1 si la validación encuentra un error. Los avisos no frenan.
Solo stdlib.
"""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
PLANTILLA_HTML = AQUI / "lienzo.html"

TIPOS = {"disparador", "espera", "condicion", "plantilla", "agente", "accion", "corte", "fin", "nota"}
ESTADOS = {"vigente", "sin_activar", "propuesto", "espera", "hueco"}
CAMBIOS = {"nuevo", "cambia", "sale", "igual"}
TIPOS_ARISTA = {"", "si", "no", "corte"}
CATEGORIAS = {"Marketing", "Utilidad", "Autenticación"}
PENDIENTES = {"a_medir": "Falta medirlo", "a_decidir": "Falta decidirlo"}

# ── Vocabulario de setter-flujo/v1 (docs/contracts/flow-definition-v1.md del producto) ──
EVENTOS = {
    "carrito": ("Abandonó el carrito", "Hotmart · PURCHASE_OUT_OF_SHOPPING_CART"),
    "pago_fallido": ("Le rechazaron el pago", "Hotmart · PURCHASE_CANCELED"),
    "ficha_impresa": ("Generó una ficha de pago", "Hotmart · PURCHASE_BILLET_PRINTED"),
    "ficha_vencida": ("Se le venció la ficha", "Hotmart · aviso de vencimiento"),
    "compra": ("Compró", "Hotmart · PURCHASE_APPROVED"),
    "intencion": ("Llenó el formulario", "Landing · intención con consentimiento"),
}
PERMISOS = {"formulario": "consintió en el formulario", "conversacion": "ya conversó con el agente", "compra": "compró"}
SALIDAS = {"compra": "compra", "contesta": "contesta", "ficha_vencida": "vence la ficha"}
# Lo que corta siempre desde el código, sin escribirse en la definición (contrato :152-182).
CORTES_DEL_CODIGO = [
    "la baja («No más mensajes»)",
    "una persona del equipo toma la conversación",
    "la persona responde después del primer envío",
    "le gana otro flujo de más prioridad",
    "no queda horario antes de vencer",
    "ya salieron todos los pasos",
]
VARIABLES = {"nombre": "nombre", "producto": "producto", "medio_de_pago": "medio de pago"}

# Lo que nunca entra a una página que se comparte: teléfonos, mails y claves.
PATRON_NUMERO = re.compile(r"(?<![\w.#/=:-])\+?\d[\d \-()]{8,}\d(?![\w-])")
PATRON_MAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PATRON_CLAVE = re.compile(r"\b(sk-[A-Za-z0-9]{12,}|xox[bp]-[\w-]{10,}|eyJ[\w-]{20,}\.[\w-]{10,}|EAA[A-Za-z0-9]{30,})")


def dur(v) -> str:
    if isinstance(v, str) and v in PENDIENTES:
        return PENDIENTES[v]
    m = re.fullmatch(r"(\d+)(m|h|d)", str(v))
    if not m:
        return str(v)
    n, u = int(m.group(1)), m.group(2)
    if n == 0:
        return "Enseguida"
    if u == "m" and n % 60 == 0:
        n, u = n // 60, "h"
    if u == "h" and n % 24 == 0 and n >= 48:
        n, u = n // 24, "d"
    return {"m": f"{n} min", "h": f"{n} h", "d": f"{n} día" if n == 1 else f"{n} días"}[u]


# ── El catálogo de plantillas ──

def leer_catalogo(ruta: Path) -> dict:
    """El export de message_templates de Chatwoot (evidencia/*catalogo-plantillas*.json de la instancia)."""
    d = json.loads(ruta.read_text(encoding="utf-8"))
    out = {}
    # Dos formas de export: la de psql (`templates`) y la de la API de Chatwoot (`message_templates`).
    lista = d.get("templates") or d.get("message_templates") or []
    if not lista:
        raise SystemExit(f"{ruta}: no tiene `templates` ni `message_templates`; ¿es un catálogo de Chatwoot?")
    for t in lista:
        cuerpo, botones = "", []
        for c in t.get("components", []):
            if c.get("type") == "BODY":
                cuerpo = c.get("text", "")
            if c.get("type") == "BUTTONS":
                for b in c.get("buttons", []):
                    botones.append({"texto": b.get("text", ""), "tipo": "url" if b.get("type") == "URL" else "respuesta",
                                    **({"destino": b["url"]} if b.get("url") else {})})
        out[t["name"]] = {
            "texto": cuerpo, "botones": botones, "idioma": t.get("language"),
            "categoria": {"MARKETING": "Marketing", "UTILITY": "Utilidad", "AUTHENTICATION": "Autenticación"}.get(t.get("category"), t.get("category")),
            "aprobacion": t.get("status"), "evidencia": [{"ref": f"{ruta.name} · {t['name']}", "nota": f"{t.get('status')} en el catálogo de Chatwoot" + (f", leído el {d['_capture']['captured_at']}" if (d.get("_capture") or {}).get("captured_at") else "")}],
        }
    return out


# ── La traducción de setter-flujo/v1 a nodos ──

def traducir(defi: dict, plantillas: dict, estado: str) -> tuple[list, list]:
    nodos, aristas = [], []
    termina = defi.get("termina") or {}
    corta = [SALIDAS.get(x, x) for x in termina.get("si") or []] + ["baja"]

    ev = defi.get("dispara") or {}
    if "evento" in ev:
        tit, sub = EVENTOS.get(ev["evento"], (ev["evento"], ""))
    else:
        s = ev.get("silencio") or {}
        quien = "el agente" if s.get("lo_ultimo") == "agente" else "la persona"
        tit = f"Silencio: lo último lo dijo {quien}"
        sub = f"entre {dur(s.get('desde'))} y {dur(s.get('hasta'))} · conversaciones {', '.join(s.get('conversaciones') or [])}"
    entra = defi.get("entra") or {}
    horario = defi.get("horario")
    topes = defi.get("topes") or {}
    rep = topes.get("repeticion") or {}
    hor_txt = "siempre" if horario == "siempre" else (f"{horario['desde']}–{horario['hasta']} ({horario['zona']})" if isinstance(horario, dict) else "")
    datos = [["Entra si", PERMISOS.get(entra.get("permiso"), entra.get("permiso") or "")]]
    if entra.get("contesto_a"):
        datos.append(["Contestó a", ", ".join(entra["contesto_a"])])
    if hor_txt:
        datos.append(["Horario", hor_txt])
    if topes:
        datos.append(["Topes", f"{topes.get('por_dia')} por día · {topes.get('total')} en total · bolsa {topes.get('bolsa')}".replace("sin_tope", "sin tope")])
    if rep:
        otra = rep.get("otra_despues_de")
        otra_txt = "no se repite nunca" if otra == "nunca" else "puede repetirse enseguida" if dur(otra) == "Enseguida" else f"puede repetirse a {dur(otra)}"
        datos.append(["Una vez por", f"{rep.get('una_por', '').replace('_', ' ')} · {otra_txt}"])
    pr = defi.get("prioridad") or {}
    if pr.get("le_gana_a"):
        datos.append(["Le gana a", ", ".join(pr["le_gana_a"])])
    if pr.get("no_arranca_mientras"):
        datos.append(["No arranca mientras", ", ".join(pr["no_arranca_mientras"])])
    chips = [PERMISOS.get(entra.get("permiso"), "")] if entra.get("permiso") else []
    if hor_txt:
        chips.append(hor_txt.split(" (")[0])
    if topes.get("por_dia") not in (None, "sin_tope"):
        chips.append(f"{topes['por_dia']}/día")
    nodos.append({"id": "dispara", "tipo": "disparador", "titulo": tit, "sub": sub, "estado": estado, "chips": chips, "datos": datos})

    def plantilla_nodo(nid, paso, primero_envio):
        ref = paso["plantilla"]
        a_crear = isinstance(ref, dict)
        nombre = ref.get("a_crear") if a_crear else ref
        p = copy.deepcopy(plantillas.get(nombre) or {})
        p.setdefault("variables", [VARIABLES.get(v, v) for v in paso.get("variables") or []])
        p["nombre"] = nombre
        p.setdefault("idioma", paso.get("idioma"))
        if paso.get("boton"):
            b = paso["boton"]
            destino = {"checkout": "link de pago emitido", "ficha": "la ficha de pago (billet_url)"}.get(b.get("link"), b.get("link"))
            if b.get("cupon"):
                destino += f" · cupón {b['cupon']}"
            urls = [x for x in p.get("botones") or [] if x.get("tipo") == "url"]
            if urls:
                urls[0]["destino"] = (urls[0].get("destino", "") + " → " + destino).strip(" →")
        n = {"id": nid, "tipo": "plantilla", "titulo": p.pop("titulo", None) or nombre, "estado": p.pop("estado", None) or estado,
             "sub": p.pop("sub", None) or ("Primer envío" if primero_envio else "Si no contestó ni compró"), "plantilla": p}
        ev_p = p.pop("evidencia", None)
        if ev_p:
            n["evidencia"] = ev_p
        aprob = p.pop("aprobacion", None)
        if a_crear:
            n["pendiente"] = {"corto": "Falta crearla y que Meta la apruebe", "texto": f"La plantilla {nombre} no existe todavía: hay que crearla en el WhatsApp Manager y esperar la aprobación de Meta."}
            if n["estado"] == "vigente":
                n["estado"] = "propuesto"
        elif aprob and aprob != "APPROVED":
            n["pendiente"] = {"corto": f"Meta: {aprob}", "texto": f"La plantilla figura {aprob} en el catálogo."}
        if not p.get("texto"):
            n["supuesto"] = "El texto no está en el catálogo ni en la fuente: se muestra solo el nombre."
            p["texto"] = f"(texto de {nombre} sin cargar)"
        return n

    contador = {"envios": 0, "esperas": 0}

    def recorrer(pasos, previos, prefijo):
        """previos: lista de (id, etiqueta) desde los que sale la flecha al paso siguiente."""
        for i, paso in enumerate(pasos):
            nid = f"{prefijo}{i}"
            if "esperar" in paso:
                v = paso["esperar"]
                desde = "desde el aviso" if contador["envios"] == 0 else "desde el envío anterior"
                n = {"id": nid, "tipo": "espera", "titulo": dur(v), "sub": desde, "estado": estado, "corta_si": corta, "espera": f"{dur(v)} {desde}"}
                if v in PENDIENTES:
                    n["estado"] = "espera"
                    n["pendiente"] = {"corto": PENDIENTES[v], "texto": f"La espera figura como `{v}` en la definición."}
                contador["esperas"] += 1
            elif "plantilla" in paso:
                n = plantilla_nodo(nid, paso, contador["envios"] == 0)
                contador["envios"] += 1
            elif "condicion" in paso:
                c = paso["condicion"]
                if c.get("dato") == "ventana":
                    tit = "¿La ventana de 24 h está abierta?"
                elif c.get("dato") == "medio_de_pago":
                    tit = f"¿Pagó con {' o '.join(c.get('es') or [])}?".replace("CASHPAYMENT", "OXXO")
                else:
                    tit = f"¿{c.get('dato')} es {', '.join(c.get('es') or [])}?"
                n = {"id": nid, "tipo": "condicion", "titulo": tit, "estado": estado, "condicion": f"{c.get('dato')} ∈ {c.get('es')}"}
                nodos.append(n)
                for de, et in previos:
                    aristas.append({"de": de, "a": nid, "etiqueta": et})
                envios_antes = contador["envios"]
                ramas = []
                for clave, etq, suf in (("entonces", "sí", "s"), ("si_no", "no", "n")):
                    contador["envios"] = envios_antes
                    if paso.get(clave):
                        ramas += recorrer(paso[clave], [(nid, etq)], f"{nid}{suf}")
                    else:
                        # Una rama vacía termina en su propio nodo, para que la flecha no cruce el lienzo.
                        vid = f"{nid}{suf}0"
                        nodos.append({"id": vid, "tipo": "fin", "titulo": "No se le escribe", "sub": f"rama «{etq}» vacía", "estado": estado})
                        aristas.append({"de": nid, "a": vid, "etiqueta": etq})
                previos = ramas
                continue
            elif "conversar" in paso:
                n = {"id": nid, "tipo": "agente", "titulo": "Conversa el agente", "sub": paso["conversar"], "estado": estado, "detalle": f"Objetivo: {paso['conversar']}"}
                nodos.append(n)
                for de, et in previos:
                    aristas.append({"de": de, "a": nid, "etiqueta": et})
                return []
            elif "derivar" in paso:
                n = {"id": nid, "tipo": "fin", "titulo": "Pasa a una persona", "sub": paso["derivar"], "estado": estado, "detalle": f"Nota para el equipo: {paso['derivar']}"}
                nodos.append(n)
                for de, et in previos:
                    aristas.append({"de": de, "a": nid, "etiqueta": et})
                return []
            else:
                n = {"id": nid, "tipo": "nota", "titulo": "Paso desconocido", "estado": "hueco", "detalle": json.dumps(paso, ensure_ascii=False)}
            nodos.append(n)
            for de, et in previos:
                aristas.append({"de": de, "a": nid, "etiqueta": et})
            previos = [(nid, "")]
        return previos

    finales = recorrer(defi.get("pasos") or [], [("dispara", "")], "p")
    if finales:
        vence = termina.get("vence")
        nodos.append({
            "id": "fin", "tipo": "fin", "titulo": "Termina la secuencia",
            "sub": (f"plazo: {PENDIENTES[vence].lower()}" if vence in PENDIENTES else f"vence a {dur(vence).lower()} del aviso" if vence else "sin plazo")
                   + f" · corta si {', '.join(corta)}",
            "estado": estado,
            "detalle": "Corta antes de terminar si pasa algo de esto. Escrito en la definición: "
                       + (", ".join(SALIDAS.get(x, x) for x in termina.get("si") or []) or "nada")
                       + ". Desde el código, siempre: " + "; ".join(CORTES_DEL_CODIGO) + ".",
        })
        for de, et in finales:
            aristas.append({"de": de, "a": "fin", "etiqueta": et})
    return nodos, aristas


def _clave(n: dict):
    p = n.get("plantilla") or {}
    extremos = n.get("tipo") in ("disparador", "fin")
    return (n.get("tipo"), n.get("titulo"), p.get("nombre"), n.get("espera"), n.get("condicion"),
            json.dumps(n.get("datos"), ensure_ascii=False) if extremos else None, n.get("sub") if extremos else None)


def marcar_cambios(hoy: list, nueva: list) -> None:
    """igual = el mismo paso; cambia = el paso del mismo tipo en el mismo orden, con otro contenido; nuevo = no estaba."""
    claves_hoy = {_clave(n) for n in hoy}
    por_tipo: dict = {}
    for n in hoy:
        por_tipo.setdefault(n.get("tipo"), []).append(n)
    vistos: dict = {}
    for n in nueva:
        if "cambio" in n:
            continue
        t = n.get("tipo")
        orden = vistos.get(t, 0)
        vistos[t] = orden + 1
        if _clave(n) in claves_hoy:
            n["cambio"] = "igual"
        elif orden < len(por_tipo.get(t, [])):
            n["cambio"] = "cambia"
        else:
            n["cambio"] = "nuevo"


def construir(fuente: dict, catalogo: dict) -> dict:
    """De la fuente al JSON que lee el lienzo."""
    d = copy.deepcopy(fuente)
    plantillas = {**catalogo, **(d.pop("plantillas", None) or {})}
    for nombre, p in plantillas.items():
        if nombre in catalogo and nombre in (fuente.get("plantillas") or {}):
            plantillas[nombre] = {**catalogo[nombre], **fuente["plantillas"][nombre]}
    for f in d.get("flujos") or []:
        for vn, v in (f.get("versiones") or {}).items():
            estado = v.get("estado") or ("vigente" if vn == "hoy" else "propuesto")
            v["estado"] = estado
            if v.get("definicion"):
                nodos, aristas = traducir(v["definicion"], plantillas, estado)
                v["nodos"] = nodos + (v.get("nodos") or [])
                v["aristas"] = aristas + (v.get("aristas") or [])
                v.pop("definicion")
            for n in v.get("nodos") or []:
                extra = (v.get("nodos_extra") or {}).get(n["id"]) or {}
                for k, val in extra.items():
                    if k == "evidencia":
                        n["evidencia"] = (n.get("evidencia") or []) + val
                    else:
                        n[k] = val
                if not n.get("evidencia") and v.get("evidencia") and n.get("tipo") != "nota":
                    n["evidencia"] = copy.deepcopy(v["evidencia"])
                elif v.get("evidencia") and n.get("tipo") == "plantilla":
                    n["evidencia"] = n["evidencia"] + [e for e in v["evidencia"] if e not in n["evidencia"]]
            v.pop("nodos_extra", None)
        # Qué cambia contra «hoy»
        hoy = ((f.get("versiones") or {}).get("hoy") or {}).get("nodos") or []
        for vn, v in (f.get("versiones") or {}).items():
            if vn != "hoy":
                marcar_cambios(hoy, v.get("nodos") or [])
    return d


SIM_ESQUEMA = "setter-simulacion-lienzo/v1"


def _dias(ventana: dict) -> int:
    from datetime import datetime
    a = datetime.fromisoformat(ventana["desde"].replace("Z", "+00:00"))
    b = datetime.fromisoformat(ventana["hasta"].replace("Z", "+00:00"))
    return max(1, round((b - a).total_seconds() / 86400))


def aplicar_simulacion(d: dict, sim: dict) -> list[str]:
    """Pone los conteos simulados en los nodos de plantilla. Devuelve los errores."""
    if sim.get("esquema") != SIM_ESQUEMA:
        return [f"la simulación no es {SIM_ESQUEMA}: {sim.get('esquema')!r}"]
    errores = []
    dias = _dias(sim["ventana"])
    flujos = {f["id"]: f for f in d.get("flujos") or []}
    for fid, versiones in (sim.get("flujos") or {}).items():
        f = flujos.get(fid)
        if f is None:
            errores.append(f"simulación: el flujo `{fid}` no está en el lienzo")
            continue
        for vn, sv in versiones.items():
            v = (f.get("versiones") or {}).get(vn)
            if v is None:
                errores.append(f"simulación: `{fid}` no tiene la versión `{vn}` en el lienzo")
                continue
            nodos = {n["id"]: n for n in v.get("nodos") or []}
            for paso in sv.get("pasos") or []:
                n = nodos.get(f"p{paso['paso']}")
                if n is None or n.get("tipo") != "plantilla":
                    errores.append(f"simulación: `{fid}`/`{vn}` paso {paso['paso']} no es una plantilla del lienzo: "
                                   "la propuesta simulada no es la definición que dibuja el lienzo")
                    continue
                cota = " como máximo" if paso.get("cota") == "como_maximo" else ""
                texto = f"{paso['enviados']}{cota} en {dias} días"
                movidos = paso.get("movidos_por_el_tope") or 0
                n["simulado"] = texto + (f" · {movidos} movidos por el tope" if movidos else "")
            fin = nodos.get("fin")
            if fin is not None and sv.get("recorridos") is not None:
                fin.setdefault("datos", []).append(["Simulado", f"{sv['recorridos']} recorridos en {dias} días, {sv.get('en_curso', 0)} en curso al cierre"])
    d["simulacion"] = {"captura": sim.get("captura"), "dias": dias, "aviso": sim.get("aviso"),
                       "legado_cambio": bool(sim.get("legado_cambio"))}
    return errores


# ── Validación ──

def _textos(obj, ruta="$"):
    if isinstance(obj, str):
        yield ruta, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _textos(v, f"{ruta}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _textos(v, f"{ruta}[{i}]")


def validar(d: dict) -> tuple[list[str], list[str]]:
    errores: list[str] = []
    avisos: list[str] = []
    for campo in ("instancia", "titulo", "medido_el", "flujos"):
        if not d.get(campo):
            errores.append(f"falta `{campo}` en la raíz")
    if not d.get("fuentes"):
        avisos.append("no hay `fuentes`: la página no dice de dónde salió")

    for ruta, t in _textos(d):
        if PATRON_MAIL.search(t):
            errores.append(f"{ruta}: parece un mail; los datos personales no entran")
        if PATRON_CLAVE.search(t):
            errores.append(f"{ruta}: parece una clave o un token")
        for m in PATRON_NUMERO.finditer(t):
            digitos = re.sub(r"\D", "", m.group())
            # 10 a 13 dígitos es un teléfono (con o sin lada); 14 es una migración (AAAAMMDDhhmmss).
            if 10 <= len(digitos) <= 13:
                errores.append(f"{ruta}: parece un teléfono ({len(digitos)} dígitos)")

    for i, r in enumerate(d.get("reglas") or []):
        if not r.get("nombre"):
            errores.append(f"reglas[{i}]: falta `nombre`")
        if r.get("estado") and r["estado"] not in ESTADOS:
            errores.append(f"reglas[{i}]: estado `{r['estado']}` no existe")
        if r.get("estado") in ("vigente", "hueco") and not r.get("evidencia"):
            errores.append(f"reglas[{i}] ({r.get('nombre')}): dice `{r['estado']}` y no tiene `evidencia`")

    vistas = set(d.get("vistas") or ["hoy", "propuesto"])
    ids_flujo = set()
    for fi, f in enumerate(d.get("flujos") or []):
        donde = f"flujos[{fi}] ({f.get('id', '?')})"
        if not f.get("id") or not f.get("nombre"):
            errores.append(f"{donde}: falta `id` o `nombre`")
        if f.get("id") in ids_flujo:
            errores.append(f"{donde}: `id` repetido")
        ids_flujo.add(f.get("id"))
        versiones = f.get("versiones") or {}
        if not versiones:
            errores.append(f"{donde}: no tiene `versiones`")
        for vn, v in versiones.items():
            dv = f"{donde}.{vn}"
            if vn not in vistas:
                errores.append(f"{dv}: la versión no está en `vistas` de la raíz")
            if v.get("estado") and v["estado"] not in ESTADOS:
                errores.append(f"{dv}: estado `{v['estado']}` no existe")
            nodos = v.get("nodos") or []
            if not nodos and not v.get("vacio"):
                avisos.append(f"{dv}: sin nodos y sin `vacio` que explique por qué")
            ids = [n.get("id") for n in nodos]
            if len(ids) != len(set(ids)):
                errores.append(f"{dv}: hay ids de nodo repetidos")
            ids_set = set(ids)
            entrantes = {i: 0 for i in ids}
            for ai, a in enumerate(v.get("aristas") or []):
                for lado in ("de", "a"):
                    if a.get(lado) not in ids_set:
                        errores.append(f"{dv}.aristas[{ai}]: `{lado}` apunta a `{a.get(lado)}`, que no existe")
                if a.get("tipo", "") not in TIPOS_ARISTA:
                    errores.append(f"{dv}.aristas[{ai}]: tipo `{a.get('tipo')}` no existe")
                if a.get("a") in entrantes:
                    entrantes[a["a"]] += 1
            for n in nodos:
                dn = f"{dv}.{n.get('id')}"
                if not n.get("titulo"):
                    errores.append(f"{dn}: falta `titulo`")
                if n.get("tipo") not in TIPOS:
                    errores.append(f"{dn}: tipo `{n.get('tipo')}` no existe")
                if n.get("estado") not in ESTADOS:
                    errores.append(f"{dn}: estado `{n.get('estado')}` no existe")
                if n.get("cambio") and n["cambio"] not in CAMBIOS:
                    errores.append(f"{dn}: cambio `{n['cambio']}` no existe")
                # La regla que hace confiable la página: lo que dice que corre, lo prueba.
                if n.get("estado") in ("vigente", "sin_activar", "hueco") and n.get("tipo") != "nota" and not n.get("evidencia"):
                    errores.append(f"{dn}: dice `{n['estado']}` y no tiene `evidencia` (archivo:línea, consulta o documento)")
                if n.get("tipo") == "plantilla":
                    p = n.get("plantilla") or {}
                    if not p.get("texto"):
                        errores.append(f"{dn}: plantilla sin `texto`")
                    if p.get("categoria") and p["categoria"] not in CATEGORIAS:
                        avisos.append(f"{dn}: categoría `{p['categoria']}` (se esperaba {', '.join(sorted(CATEGORIAS))})")
                    usadas = {int(x) for x in re.findall(r"\{\{(\d+)\}\}", p.get("texto", ""))}
                    if usadas and max(usadas) > len(p.get("variables") or []):
                        errores.append(f"{dn}: el texto usa {{{{{max(usadas)}}}}} y `variables` tiene {len(p.get('variables') or [])}")
                    if len(p.get("botones") or []) > 10:
                        errores.append(f"{dn}: más de 10 botones (el tope de Meta)")
                    if sum(1 for b in p.get("botones") or [] if b.get("tipo") == "url") > 2:
                        errores.append(f"{dn}: más de 2 botones de URL (el tope de Meta)")
                    if p.get("categoria") == "Marketing" and not any(b.get("texto") == "No más mensajes" for b in p.get("botones") or []):
                        avisos.append(f"{dn}: plantilla de marketing sin el botón «No más mensajes»")
                    if n.get("supuesto", "").startswith("El texto no está"):
                        avisos.append(f"{dn}: sin texto de plantilla (falta el catálogo o la entrada en `plantillas`)")
                if n.get("tipo") != "disparador" and entrantes.get(n.get("id"), 0) == 0 and n.get("tipo") != "nota":
                    avisos.append(f"{dn}: no le llega ninguna flecha")
                if n.get("pendiente") and not (n["pendiente"].get("texto") or n["pendiente"].get("corto")):
                    errores.append(f"{dn}: `pendiente` sin texto")
    return errores, avisos


def armar_html(d: dict) -> str:
    plantilla = PLANTILLA_HTML.read_text(encoding="utf-8")
    datos = json.dumps(d, ensure_ascii=False, indent=1).replace("</", "<\\/")
    titulo = (d.get("titulo_pagina") or d.get("titulo") or "Flujos").replace("&", "&amp;").replace("<", "&lt;")
    return plantilla.replace("__TITULO__", titulo, 1).replace("__DATOS__", datos, 1)


def _selftest() -> int:
    defi = {"esquema": "setter-flujo/v1", "flujo": "x", "dispara": {"evento": "ficha_impresa"}, "entra": {"permiso": "formulario"},
            "pasos": [{"esperar": "60m"}, {"condicion": {"dato": "medio_de_pago", "es": ["CASHPAYMENT"]},
                       "entonces": [{"plantilla": {"a_crear": "c1"}, "variables": ["nombre"], "boton": {"link": "ficha"}}, {"esperar": "a_medir"}, {"plantilla": "c2", "variables": ["nombre"]}],
                       "si_no": []}],
            "termina": {"si": ["compra", "ficha_vencida"], "vence": "5d"}}
    fuente = {"instancia": "prueba", "titulo": "Flujos de prueba", "medido_el": "2026-10-17", "fuentes": [{"ref": "doc.md"}],
              "vistas": ["hoy", "propuesto"],
              "plantillas": {"c1": {"texto": "Hola {{1}}", "categoria": "Utilidad", "botones": [{"texto": "Ver mi ficha", "tipo": "url"}]},
                             "c2": {"texto": "Vence hoy, {{1}}", "categoria": "Utilidad"}},
              "flujos": [{"id": "f", "nombre": "F", "versiones": {"propuesto": {"definicion": defi}}}]}
    d = construir(fuente, {})
    v = d["flujos"][0]["versiones"]["propuesto"]
    ids = [n["id"] for n in v["nodos"]]
    assert ids == ["dispara", "p0", "p1", "p1s0", "p1s1", "p1s2", "p1n0", "fin"], ids
    assert {"de": "p1", "a": "p1n0", "etiqueta": "no"} in v["aristas"], v["aristas"]
    assert not any(a["de"] == "p1" and a["a"] == "fin" for a in v["aristas"])
    assert {"de": "p1", "a": "p1s0", "etiqueta": "sí"} in v["aristas"]
    assert next(n for n in v["nodos"] if n["id"] == "p1s1")["estado"] == "espera"
    assert next(n for n in v["nodos"] if n["id"] == "p1s0")["pendiente"]
    assert all(n["cambio"] == "nuevo" for n in v["nodos"])
    e, _ = validar(d)
    assert not e, e
    # Lo que dice que corre tiene que probarlo.
    malo = copy.deepcopy(d)
    malo["flujos"][0]["versiones"]["propuesto"]["nodos"][0]["estado"] = "vigente"
    assert any("evidencia" in x for x in validar(malo)[0])
    malo = copy.deepcopy(d)
    malo["flujos"][0]["versiones"]["propuesto"]["nodos"][1]["detalle"] = "llamó del +52 55 1234 5678"
    assert any("teléfono" in x for x in validar(malo)[0])
    malo = copy.deepcopy(d)
    malo["flujos"][0]["versiones"]["propuesto"]["aristas"].append({"de": "p0", "a": "zz"})
    assert any("zz" in x for x in validar(malo)[0])
    # Una migración, una oferta, un hash o una ruta con número de línea no son teléfonos.
    ok = copy.deepcopy(d)
    ok["flujos"][0]["versiones"]["propuesto"]["nodos"][0]["detalle"] = "migración 20260808000100:202, oferta 2uafw5bg, 9fc0a22, app.py:4752-4799"
    assert not validar(ok)[0], validar(ok)[0]
    # «cambio» contra hoy
    f2 = copy.deepcopy(fuente)
    f2["flujos"][0]["versiones"]["hoy"] = {"definicion": {**defi, "pasos": [{"esperar": "60m"}]}, "evidencia": [{"ref": "x.sql:1"}]}
    d2 = construir(f2, {})
    cambios = {n["id"]: n["cambio"] for n in d2["flujos"][0]["versiones"]["propuesto"]["nodos"]}
    assert cambios["p0"] == "igual" and cambios["p1"] == "nuevo", cambios
    assert dur("60m") == "1 h" and dur("48h") == "2 días" and dur("1d") == "1 día" and dur("0m") == "Enseguida"
    # La simulación va en `simulado`, nunca en `metrica`, y solo sobre una plantilla.
    sim = {"esquema": SIM_ESQUEMA, "ventana": {"desde": "2026-10-08T22:00:00Z", "hasta": "2026-10-16T21:00:00Z"},
           "flujos": {"f": {"propuesto": {"recorridos": 9, "en_curso": 1, "pasos": [
               {"paso": 1, "enviados": 7, "cota": "como_maximo", "movidos_por_el_tope": 2}]}}}}
    ds = construir(fuente, {})
    assert any("plantilla" in x for x in aplicar_simulacion(copy.deepcopy(ds), sim))
    sim["flujos"]["f"]["propuesto"]["pasos"][0]["paso"] = 0
    assert any("plantilla" in x for x in aplicar_simulacion(copy.deepcopy(ds), sim))
    ds["flujos"][0]["versiones"]["propuesto"]["nodos"].append({"id": "p9", "tipo": "plantilla", "titulo": "t", "estado": "propuesto"})
    sim["flujos"]["f"]["propuesto"]["pasos"][0]["paso"] = 9
    assert not aplicar_simulacion(ds, sim)
    n9 = next(n for n in ds["flujos"][0]["versiones"]["propuesto"]["nodos"] if n["id"] == "p9")
    assert n9["simulado"] == "7 como máximo en 8 días · 2 movidos por el tope" and "metrica" not in n9, n9
    assert aplicar_simulacion(copy.deepcopy(ds), {**sim, "esquema": "otro"})
    html = armar_html(d)
    assert "__DATOS__" not in html and "Flujos de prueba" in html
    print("selftest ok")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--selftest"]:
        return _selftest()
    if not argv:
        print(__doc__)
        return 2
    args = list(argv)
    catalogo, lienzo_json = {}, None
    while "--catalogo" in args:
        i = args.index("--catalogo")
        catalogo.update(leer_catalogo(Path(args[i + 1]).expanduser()))
        del args[i:i + 2]
    if "--lienzo-json" in args:
        i = args.index("--lienzo-json")
        lienzo_json = Path(args[i + 1])
        del args[i:i + 2]
    defs_hoy = None
    if "--definiciones-hoy" in args:
        i = args.index("--definiciones-hoy")
        crudo = json.loads(Path(args[i + 1]).expanduser().read_text(encoding="utf-8"))
        lista = crudo.get("definitions") if isinstance(crudo, dict) else crudo
        defs_hoy = {x.get("flujo"): x.get("definition", x) if "definition" in x else x for x in lista}
        del args[i:i + 2]
    sim = None
    if "--simulacion" in args:
        i = args.index("--simulacion")
        sim = json.loads(Path(args[i + 1]).expanduser().read_text(encoding="utf-8"))
        del args[i:i + 2]
    ruta_fuente = Path(args[0])
    fuente = json.loads(ruta_fuente.read_text(encoding="utf-8"))
    if defs_hoy is not None:
        cambiados = []
        for f in fuente.get("flujos") or []:
            hoy = (f.get("versiones") or {}).get("hoy")
            nueva = defs_hoy.pop(f.get("id"), None)
            if hoy is None or nueva is None:
                continue
            if hoy.get("definicion") != nueva:
                cambiados.append(f["id"])
                hoy["definicion"] = nueva
        for sobra in defs_hoy:
            print(f"aviso: la definición de `{sobra}` no tiene flujo en la fuente: agregalo si corre en la instancia")
        if cambiados:
            ruta_fuente.write_text(json.dumps(fuente, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"definiciones de hoy actualizadas en la fuente: {', '.join(cambiados)}")
        else:
            print("definiciones de hoy: sin cambios")
    d = construir(fuente, catalogo)
    errores_sim = aplicar_simulacion(d, sim) if sim is not None else []
    if sim is not None and sim.get("aviso"):
        print(f"aviso: {sim['aviso']}")
    errores, avisos = validar(d)
    errores = errores_sim + errores
    for a in avisos:
        print(f"aviso: {a}")
    for e in errores:
        print(f"ERROR: {e}")
    if errores:
        print(f"{len(errores)} errores: no se arma la página")
        return 1
    if lienzo_json:
        lienzo_json.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    if args[1:2] == ["--solo-validar"] or len(args) < 2:
        print("válido")
        return 0
    salida = Path(args[1])
    salida.write_text(armar_html(d), encoding="utf-8")
    n = sum(len(v.get("nodos") or []) for f in d["flujos"] for v in (f.get("versiones") or {}).values())
    print(f"armado: {salida} ({len(d['flujos'])} flujos, {n} nodos, {len(avisos)} avisos)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
