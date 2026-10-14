#!/usr/bin/env python3
"""Deja el Hermes de una instancia igual a lo que declara el producto, y chequea que siga igual.

ADR-0025. Corre en el servidor de la instancia, desde un checkout del producto en la version que
usa la instancia, con permiso para usar docker:

    python3 scripts/hermes_instancia.py plan     --instancia /opt/setter-att1/instancia/hermes.toml
    python3 scripts/hermes_instancia.py aplicar  --instancia /opt/setter-att1/instancia/hermes.toml
    python3 scripts/hermes_instancia.py chequear --instancia /opt/setter-att1/instancia/hermes.toml

- ``plan`` dice que haria ``aplicar`` y no escribe nada. Hermes lee el SOUL y el config del
  profile en cada pedido, asi que escribir en un profile que esta atendiendo cambia al agente en
  el pedido siguiente, sin reiniciar (medido el 2026-10-14): ``aplicar`` es un deploy.
- ``aplicar`` crea lo que falta y pisa lo que difiere de Git. Correrlo dos veces da lo mismo. Nunca
  borra un profile ni toca memorias, sesiones o registros. No reinicia un gateway que ya atiende,
  salvo que cambie su puerto y se pida con ``--reiniciar``.
- ``chequear`` compara lo que corre con lo declarado y sale con 1 ante cualquier diferencia.

Frena (sale con 2) sin tocar nada si:
- el contenedor no es del stack que declara la instancia (etiqueta ``com.docker.stack.namespace``);
- con ``aplicar``, la imagen no es la que declara el producto;
- el Hermes tiene un profile que no instalo este script y no tiene la marca de la instancia;
- un secreto no esta en el archivo que declara la instancia.

Las keys nunca se imprimen: se leen de los archivos de secretos del servidor, entran al contenedor
por stdin y, para comparar, se usa su hash.

El mismo archivo corre adentro del contenedor (``--dentro``) con el python de Hermes, que trae
PyYAML y la funcion de Hermes que calcula las herramientas efectivas de un profile. Afuera alcanza
con la libreria estandar (Python 3.11 o mas nuevo, por ``tomllib``).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parents[1]
DECLARACION = RAIZ / "hermes" / "declaracion.toml"
ESQUEMA = 1
AUTOR = "ai-appointment-setter"
ARCHIVOS_DISTRIBUCION = ("SOUL.md", "config.yaml", "distribution.yaml")

DATOS = Path("/opt/data")
PROFILES = DATOS / "profiles"
MARCA = DATOS / ".setter-instancia.json"
PYTHON_HERMES = "/opt/hermes/.venv/bin/python"
HERMES = "/opt/hermes/.venv/bin/hermes"
SERVICIOS = Path("/run/service")
S6_SVSTAT = "/command/s6-svstat"
VARIABLES_DEL_PROFILE = "." + "env"

ETIQUETA_STACK = "com.docker.stack.namespace"
ETIQUETA_SERVICIO = "com.docker.swarm.service.name"

NOMBRE_PROFILE = re.compile(r"^[a-z][a-z0-9-]{1,19}$")
NOMBRE_STACK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
NOMBRE_VARIABLE = re.compile(r"^[A-Z][A-Z0-9_]*$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
VERDADERO = {"1", "true", "yes", "on"}

SALIDA_DIFERENCIA = 1
SALIDA_FRENO = 2


class Freno(Exception):
    """Algo no cierra: no se toca nada y se sale con 2."""


# --------------------------------------------------------------------------------------------
# Declaraciones (afuera del contenedor, solo libreria estandar)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Imagen:
    nombre: str
    etiqueta: str
    digest: str
    version_hermes: str

    @property
    def referencia(self) -> str:
        return f"{self.nombre}@{self.digest}"


@dataclass(frozen=True)
class ProfileProducto:
    nombre: str
    distribucion: Path
    puerto: int
    le_habla: str


@dataclass(frozen=True)
class Declaracion:
    imagen: Imagen
    profiles: dict[str, ProfileProducto]


@dataclass(frozen=True)
class Secreto:
    archivo: Path
    variable: str


@dataclass(frozen=True)
class ProfileInstancia:
    nombre: str
    modelo: str
    proveedor: str
    base_url: str | None
    respaldo: tuple[tuple[str, str], ...]
    variables: dict[str, Secreto]


@dataclass(frozen=True)
class Instancia:
    stack: str
    servicio: str
    profiles: dict[str, ProfileInstancia]


def _tabla(donde: str, valor: Any) -> dict:
    if not isinstance(valor, dict):
        raise Freno(f"{donde}: tiene que ser una tabla")
    return valor


def _claves(donde: str, tabla: dict, requeridas: set[str], opcionales: set[str] = frozenset()) -> None:
    faltan = sorted(requeridas - set(tabla))
    sobran = sorted(set(tabla) - requeridas - set(opcionales))
    if faltan:
        raise Freno(f"{donde}: faltan {', '.join(faltan)}")
    if sobran:
        raise Freno(f"{donde}: claves no soportadas {', '.join(sobran)}")


def _texto(donde: str, tabla: dict, clave: str) -> str:
    valor = tabla.get(clave)
    if not isinstance(valor, str) or not valor.strip():
        raise Freno(f"{donde}.{clave}: tiene que ser un texto no vacio")
    return valor.strip()


def _leer_toml(ruta: Path) -> dict:
    try:
        with ruta.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise Freno(f"no existe {ruta}") from None
    except tomllib.TOMLDecodeError as exc:
        raise Freno(f"{ruta}: no es TOML valido ({exc})") from None


def cargar_declaracion(ruta: Path = DECLARACION, raiz: Path = RAIZ) -> Declaracion:
    datos = _leer_toml(ruta)
    _claves("declaracion", datos, {"schema", "imagen", "profiles"})
    if datos["schema"] != ESQUEMA:
        raise Freno(f"declaracion.schema: este instalador entiende el {ESQUEMA}")
    img = _tabla("declaracion.imagen", datos["imagen"])
    _claves("declaracion.imagen", img, {"nombre", "etiqueta", "digest", "version_hermes"})
    imagen = Imagen(
        nombre=_texto("imagen", img, "nombre"),
        etiqueta=_texto("imagen", img, "etiqueta"),
        digest=_texto("imagen", img, "digest"),
        version_hermes=_texto("imagen", img, "version_hermes"),
    )
    if not DIGEST.match(imagen.digest):
        raise Freno("declaracion.imagen.digest: tiene que ser sha256:<64 hex>")
    perfiles = _tabla("declaracion.profiles", datos["profiles"])
    if not perfiles:
        raise Freno("declaracion.profiles: no declara ningun profile")
    profiles: dict[str, ProfileProducto] = {}
    puertos: set[int] = set()
    for nombre, p in perfiles.items():
        donde = f"declaracion.profiles.{nombre}"
        if not NOMBRE_PROFILE.match(nombre):
            raise Freno(f"{donde}: nombre invalido (minusculas, numeros y guiones, 2 a 20)")
        p = _tabla(donde, p)
        _claves(donde, p, {"distribucion", "puerto", "le_habla"})
        relativa = Path(_texto(donde, p, "distribucion"))
        if relativa.is_absolute() or ".." in relativa.parts:
            raise Freno(f"{donde}.distribucion: tiene que ser una ruta adentro del repo")
        carpeta = raiz / relativa
        for archivo in ARCHIVOS_DISTRIBUCION:
            if not (carpeta / archivo).is_file():
                raise Freno(f"{donde}.distribucion: falta {relativa / archivo}")
        puerto = p["puerto"]
        if not isinstance(puerto, int) or isinstance(puerto, bool) or not 1024 <= puerto <= 65535:
            raise Freno(f"{donde}.puerto: tiene que ser un entero entre 1024 y 65535")
        if puerto in puertos:
            raise Freno(f"{donde}.puerto: {puerto} ya lo usa otro profile")
        puertos.add(puerto)
        profiles[nombre] = ProfileProducto(
            nombre=nombre,
            distribucion=carpeta,
            puerto=puerto,
            le_habla=_texto(donde, p, "le_habla"),
        )
    return Declaracion(imagen=imagen, profiles=profiles)


def cargar_instancia(ruta: Path, declaracion: Declaracion) -> Instancia:
    datos = _leer_toml(ruta)
    _claves("instancia", datos, {"schema", "stack", "profiles"}, {"servicio"})
    if datos["schema"] != ESQUEMA:
        raise Freno(f"instancia.schema: este instalador entiende el {ESQUEMA}")
    stack = _texto("instancia", datos, "stack")
    if not NOMBRE_STACK.match(stack):
        raise Freno("instancia.stack: nombre de stack invalido")
    servicio = _texto("instancia", datos, "servicio") if "servicio" in datos else "hermes"
    perfiles = _tabla("instancia.profiles", datos["profiles"])
    faltan = sorted(set(declaracion.profiles) - set(perfiles))
    sobran = sorted(set(perfiles) - set(declaracion.profiles))
    if faltan:
        raise Freno(f"instancia.profiles: falta {', '.join(faltan)}, que el producto declara")
    if sobran:
        raise Freno(f"instancia.profiles: {', '.join(sobran)} no esta en la declaracion del producto")
    profiles: dict[str, ProfileInstancia] = {}
    for nombre, p in perfiles.items():
        donde = f"instancia.profiles.{nombre}"
        p = _tabla(donde, p)
        _claves(donde, p, {"modelo", "proveedor", "variables"}, {"base_url", "respaldo"})
        respaldo = []
        lista = p.get("respaldo", [])
        if not isinstance(lista, list):
            raise Freno(f"{donde}.respaldo: tiene que ser una lista")
        for i, r in enumerate(lista):
            r = _tabla(f"{donde}.respaldo[{i}]", r)
            _claves(f"{donde}.respaldo[{i}]", r, {"modelo", "proveedor"})
            respaldo.append((_texto(f"{donde}.respaldo[{i}]", r, "modelo"), _texto(f"{donde}.respaldo[{i}]", r, "proveedor")))
        variables: dict[str, Secreto] = {}
        for var, s in _tabla(f"{donde}.variables", p["variables"]).items():
            dv = f"{donde}.variables.{var}"
            if not NOMBRE_VARIABLE.match(var):
                raise Freno(f"{dv}: nombre de variable invalido")
            s = _tabla(dv, s)
            _claves(dv, s, {"archivo", "variable"})
            archivo = Path(_texto(dv, s, "archivo"))
            if not archivo.is_absolute():
                archivo = ruta.parent / archivo
            variable = _texto(dv, s, "variable")
            if not NOMBRE_VARIABLE.match(variable):
                raise Freno(f"{dv}.variable: nombre de variable invalido")
            variables[var] = Secreto(archivo=archivo, variable=variable)
        profiles[nombre] = ProfileInstancia(
            nombre=nombre,
            modelo=_texto(donde, p, "modelo"),
            proveedor=_texto(donde, p, "proveedor"),
            base_url=_texto(donde, p, "base_url") if "base_url" in p else None,
            respaldo=tuple(respaldo),
            variables=variables,
        )
    return Instancia(stack=stack, servicio=servicio, profiles=profiles)


def leer_archivo_de_variables(ruta: Path) -> dict[str, str]:
    """Lee un archivo KEY=VALUE. Ignora comentarios y lineas vacias; saca comillas de los bordes."""
    valores: dict[str, str] = {}
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        clave = clave.strip()
        if clave.startswith("export "):
            clave = clave[len("export "):].strip()
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        valores[clave] = valor
    return valores


def leer_secretos(instancia: Instancia) -> dict[str, dict[str, str]]:
    """Valor de cada variable de cada profile, leido de los archivos de secretos del servidor."""
    cache: dict[Path, dict[str, str]] = {}
    salida: dict[str, dict[str, str]] = {}
    for nombre, p in instancia.profiles.items():
        salida[nombre] = {}
        for var, s in p.variables.items():
            if s.archivo not in cache:
                if not s.archivo.is_file():
                    raise Freno(f"no existe el archivo de secretos {s.archivo} ({nombre}.{var})")
                cache[s.archivo] = leer_archivo_de_variables(s.archivo)
            valor = cache[s.archivo].get(s.variable, "")
            if not valor:
                raise Freno(f"{s.archivo} no tiene {s.variable}, que va como {var} en {nombre}")
            salida[nombre][var] = valor
    return salida


def huella(valor: str) -> str:
    return hashlib.sha256(valor.encode("utf-8")).hexdigest()


def tapar(texto: str, secretos: dict[str, dict[str, str]]) -> str:
    """Reemplaza cualquier secreto que aparezca en un texto antes de mostrarlo."""
    for por_profile in secretos.values():
        for valor in por_profile.values():
            if valor:
                texto = texto.replace(valor, "***")
    return texto


# --------------------------------------------------------------------------------------------
# El contenedor (afuera)
# --------------------------------------------------------------------------------------------


def docker(*args: str, entrada: str | None = None, chequear: bool = True) -> subprocess.CompletedProcess:
    resultado = subprocess.run(
        ["docker", *args], input=entrada, capture_output=True, text=True, check=False
    )
    if chequear and resultado.returncode != 0:
        raise Freno(f"docker {args[0]} fallo: {resultado.stderr.strip()[-300:]}")
    return resultado


def encontrar_contenedor(instancia: Instancia, explicito: str | None = None) -> str:
    """El contenedor del servicio de Hermes de la instancia. Frena si no es de su stack."""
    if explicito:
        candidato = explicito
    else:
        servicio = f"{instancia.stack}_{instancia.servicio}"
        ids = docker("ps", "--filter", f"label={ETIQUETA_SERVICIO}={servicio}", "--format", "{{.ID}}").stdout.split()
        if len(ids) != 1:
            raise Freno(f"esperaba un contenedor del servicio {servicio} y hay {len(ids)}")
        candidato = ids[0]
    formato = "{{.Id}}|{{index .Config.Labels \"" + ETIQUETA_STACK + "\"}}"
    ident, _, stack = docker("inspect", "--format", formato, candidato).stdout.strip().partition("|")
    if stack != instancia.stack:
        raise Freno(
            f"el contenedor {candidato} es del stack '{stack or 'ninguno'}', no de '{instancia.stack}': no lo toco"
        )
    return ident[:12]


def imagen_del_contenedor(contenedor: str) -> list[str]:
    imagen = docker("inspect", "--format", "{{.Image}}", contenedor).stdout.strip()
    digests = docker("image", "inspect", "--format", "{{json .RepoDigests}}", imagen).stdout.strip()
    return json.loads(digests or "[]") or []


# --------------------------------------------------------------------------------------------
# Lo que corre afuera: arma el paquete, lo pasa al contenedor y muestra el reporte
# --------------------------------------------------------------------------------------------


def armar_paquete(carpeta: Path, destino: str, modo: str, declaracion: Declaracion,
                  instancia: Instancia, reiniciar: bool) -> None:
    for nombre, producto in declaracion.profiles.items():
        dist = carpeta / "profiles" / nombre
        dist.mkdir(parents=True)
        for archivo in ARCHIVOS_DISTRIBUCION:
            fuente = producto.distribucion / archivo
            if fuente.is_symlink():
                raise Freno(f"{fuente} es un enlace: Hermes no instala distribuciones con enlaces")
            shutil.copyfile(fuente, dist / archivo)
    shutil.copyfile(Path(__file__).resolve(), carpeta / "hermes_instancia.py")
    plan = {
        "modo": modo,
        "stack": instancia.stack,
        "reiniciar": reiniciar,
        "profiles": {
            nombre: {
                "staged": f"{destino}/profiles/{nombre}",
                "puerto": declaracion.profiles[nombre].puerto,
                "modelo": p.modelo,
                "proveedor": p.proveedor,
                "base_url": p.base_url,
                "respaldo": [{"modelo": m, "proveedor": pr} for m, pr in p.respaldo],
                "variables": sorted(p.variables),
            }
            for nombre, p in instancia.profiles.items()
        },
    }
    (carpeta / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")


def correr(modo: str, ruta_instancia: Path, contenedor: str | None = None,
           reiniciar: bool = False, ruta_declaracion: Path = DECLARACION) -> int:
    declaracion = cargar_declaracion(ruta_declaracion)
    instancia = cargar_instancia(ruta_instancia, declaracion)
    secretos = leer_secretos(instancia)
    ident = encontrar_contenedor(instancia, contenedor)
    hallazgos: list[dict] = []
    digests = imagen_del_contenedor(ident)
    if declaracion.imagen.referencia in digests:
        hallazgos.append({"nivel": "ok", "texto": f"la imagen es {declaracion.imagen.nombre}:{declaracion.imagen.etiqueta} ({declaracion.imagen.digest[:19]}...)"})
    elif modo == "aplicar":
        raise Freno(f"la imagen del contenedor no es {declaracion.imagen.referencia}: no instalo sobre otra version de Hermes")
    else:
        hallazgos.append({"nivel": "diferencia", "texto": f"la imagen del contenedor no es {declaracion.imagen.referencia}"})

    destino = f"/tmp/setter-hermes-{uuid.uuid4().hex[:12]}"
    entrada: dict[str, Any] = {"huellas": {p: {v: huella(x) for v, x in vs.items()} for p, vs in secretos.items()}}
    if modo == "aplicar":
        entrada["valores"] = secretos
    with tempfile.TemporaryDirectory(prefix="setter-hermes-") as tmp:
        armar_paquete(Path(tmp), destino, modo, declaracion, instancia, reiniciar)
        try:
            docker("cp", f"{tmp}/.", f"{ident}:{destino}")
            docker("exec", ident, "chown", "-R", "hermes:hermes", destino)
            resultado = docker(
                "exec", "-i", "-u", "hermes", "-e", "HOME=/opt/data", ident,
                PYTHON_HERMES, f"{destino}/hermes_instancia.py", "--dentro", f"{destino}/plan.json",
                entrada=json.dumps(entrada), chequear=False,
            )
        finally:
            docker("exec", ident, "rm", "-rf", destino, chequear=False)
    reporte = _ultimo_json(resultado.stdout)
    if reporte is None:
        detalle = tapar((resultado.stderr or resultado.stdout).strip()[-800:], secretos)
        raise Freno(f"lo que corre adentro del contenedor se rompio:\n{detalle}")
    reporte["hallazgos"] = hallazgos + reporte.get("hallazgos", [])
    print(tapar(formatear(modo, instancia.stack, ident, reporte), secretos))
    if any(h["nivel"] == "freno" for h in reporte["hallazgos"]):
        return SALIDA_FRENO
    if modo != "plan" and any(h["nivel"] == "diferencia" for h in reporte["hallazgos"]):
        return SALIDA_DIFERENCIA
    return 0


def _ultimo_json(texto: str) -> dict | None:
    for linea in reversed(texto.strip().splitlines()):
        linea = linea.strip()
        if linea.startswith("{"):
            try:
                return json.loads(linea)
            except json.JSONDecodeError:
                return None
    return None


def formatear(modo: str, stack: str, contenedor: str, reporte: dict) -> str:
    marcas = {"ok": "  ok        ", "diferencia": "  DIFERENCIA", "freno": "  FRENO     "}
    lineas = [f"Hermes de {stack} (contenedor {contenedor}) · {modo}"]
    lineas += [f"{marcas[h['nivel']]} {h['texto']}" for h in reporte.get("hallazgos", [])]
    acciones = reporte.get("acciones", [])
    frenos = [h for h in reporte.get("hallazgos", []) if h["nivel"] == "freno"]
    diferencias = [h for h in reporte.get("hallazgos", []) if h["nivel"] == "diferencia"]
    if acciones:
        lineas.append("Aplicar haria:" if modo == "plan" else "Hecho:")
        lineas += [f"  - {a}" for a in acciones]
    elif modo == "aplicar" and not frenos:
        lineas.append("Sin cambios: ya estaba igual a la declaracion.")
    if frenos and modo == "aplicar" and acciones:
        lineas.append("VEREDICTO: FRENO a mitad de camino: lo de arriba se hizo hasta el ultimo paso, que fallo")
    elif frenos:
        lineas.append("VEREDICTO: FRENO, no se toco nada")
    elif modo == "plan":
        lineas.append(f"VEREDICTO: aplicar haria {len(acciones)} cambio(s)" if acciones else "VEREDICTO: nada que aplicar")
    elif diferencias:
        lineas.append(f"VEREDICTO: {len(diferencias)} diferencia(s) con la declaracion")
    else:
        lineas.append("VEREDICTO: igual a la declaracion")
    return "\n".join(lineas)


# --------------------------------------------------------------------------------------------
# Funciones puras que usa lo que corre adentro (y los tests)
# --------------------------------------------------------------------------------------------


def renderizar_config(molde: dict, plan_profile: dict) -> dict:
    """El config.yaml del profile: el molde con el modelo, el respaldo y el puerto puestos."""
    config = copy.deepcopy(molde)
    if not isinstance(config.get("model"), dict):
        raise Freno("el molde de config.yaml no tiene la seccion model")
    api = ((config.get("platforms") or {}).get("api_server"))
    if not isinstance(api, dict):
        raise Freno("el molde de config.yaml no tiene platforms.api_server")
    config["model"]["default"] = plan_profile["modelo"]
    config["model"]["provider"] = plan_profile["proveedor"]
    if plan_profile.get("base_url"):
        config["model"]["base_url"] = plan_profile["base_url"]
    else:
        config["model"].pop("base_url", None)
    config["fallback_providers"] = [
        {"provider": r["proveedor"], "model": r["modelo"]} for r in plan_profile.get("respaldo", [])
    ]
    api["port"] = plan_profile["puerto"]
    vacios = [ruta for ruta, valor in aplanar(config).items() if valor is None]
    if vacios:
        raise Freno(f"el config.yaml quedo con claves sin valor: {', '.join(vacios)}")
    return config


def aplanar(nodo: Any, prefijo: str = "") -> dict[str, Any]:
    """Rutas de clave -> valor. Una lista de escalares es un valor; una de tablas se recorre."""
    salida: dict[str, Any] = {}
    if isinstance(nodo, dict):
        if not nodo and prefijo:
            salida[prefijo] = {}
        for clave, valor in nodo.items():
            salida.update(aplanar(valor, f"{prefijo}.{clave}" if prefijo else str(clave)))
    elif isinstance(nodo, list) and any(isinstance(x, (dict, list)) for x in nodo):
        for i, valor in enumerate(nodo):
            salida.update(aplanar(valor, f"{prefijo}[{i}]"))
    else:
        salida[prefijo] = nodo
    return salida


def diferencias_de_config(vivo: dict, esperado: dict) -> list[str]:
    """Las rutas de clave que difieren, sin valores."""
    a, b = aplanar(vivo), aplanar(esperado)
    salida = [f"falta {r}" for r in sorted(set(b) - set(a))]
    salida += [f"sobra {r}" for r in sorted(set(a) - set(b))]
    salida += [f"distinto {r}" for r in sorted(set(a) & set(b)) if a[r] != b[r]]
    return salida


def leer_variables_de_texto(texto: str) -> dict[str, str]:
    valores: dict[str, str] = {}
    for linea in texto.splitlines():
        s = linea.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        clave, valor = s.split("=", 1)
        valores[clave.strip()] = valor.strip()
    return valores


def fusionar_variables(texto: str, deseadas: dict[str, str]) -> tuple[str, list[str]]:
    """El archivo de variables con las deseadas puestas. Conserva comentarios y las demas lineas.

    Devuelve el texto nuevo y los nombres que cambian (nunca los valores).
    """
    actuales = leer_variables_de_texto(texto)
    cambian = sorted(v for v, x in deseadas.items() if actuales.get(v) != x)
    if not cambian:
        return texto, []
    lineas = []
    puestas: set[str] = set()
    for linea in texto.splitlines():
        s = linea.strip()
        clave = s.split("=", 1)[0].strip() if "=" in s and not s.startswith("#") else None
        if clave in deseadas:
            if clave not in puestas:
                lineas.append(f"{clave}={deseadas[clave]}")
                puestas.add(clave)
            continue
        lineas.append(linea)
    for clave in sorted(deseadas):
        if clave not in puestas:
            lineas.append(f"{clave}={deseadas[clave]}")
    return "\n".join(lineas) + "\n", cambian


def profiles_presentes(raiz: Path) -> list[str]:
    """Los profiles con nombre que Hermes ve: una carpeta con SOUL.md, salvo default."""
    if not raiz.is_dir():
        return []
    return sorted(
        d.name for d in raiz.iterdir()
        if d.is_dir() and d.name != "default" and (d / "SOUL.md").is_file()
    )


def es_nuestro(distribucion: dict | None, nombre: str) -> bool:
    return bool(distribucion) and distribucion.get("author") == AUTOR and distribucion.get("name") == nombre


def verificar_duenio(stack: str, marca: dict | None, presentes: list[str], declarados: list[str],
                     distribuciones: dict[str, dict | None]) -> None:
    """Frena si este Hermes no es de la instancia. Lo usan plan y aplicar antes de escribir."""
    if marca is not None:
        if marca.get("instancia") != stack:
            raise Freno(f"este Hermes tiene la marca de la instancia '{marca.get('instancia')}', no de '{stack}': no lo toco")
        return
    ajenos = sorted(set(presentes) - set(declarados))
    if ajenos:
        raise Freno(
            f"este Hermes no tiene la marca de la instancia y tiene profiles que la declaracion no conoce ({', '.join(ajenos)}): no lo toco"
        )
    for nombre in declarados:
        if nombre in presentes and not es_nuestro(distribuciones.get(nombre), nombre):
            raise Freno(
                f"el profile {nombre} ya existe, no lo instalo este script y el Hermes no tiene la marca de la instancia: no lo piso"
            )


# --------------------------------------------------------------------------------------------
# Lo que corre adentro del contenedor, como hermes, con el python de Hermes
# --------------------------------------------------------------------------------------------


@dataclass
class Reporte:
    hallazgos: list[dict] = field(default_factory=list)
    acciones: list[str] = field(default_factory=list)

    def ok(self, texto: str) -> None:
        self.hallazgos.append({"nivel": "ok", "texto": texto})

    def diferencia(self, texto: str) -> None:
        self.hallazgos.append({"nivel": "diferencia", "texto": texto})

    def freno(self, texto: str) -> None:
        self.hallazgos.append({"nivel": "freno", "texto": texto})


class Dentro:
    def __init__(self, plan: dict, entrada: dict):
        import yaml  # el python de Hermes lo trae

        self.yaml = yaml
        self.plan = plan
        self.modo = plan["modo"]
        self.huellas = entrada.get("huellas", {})
        self.valores = entrada.get("valores", {})
        self.reporte = Reporte()
        self.esperado: dict[str, dict] = {}

    # --- lectura -------------------------------------------------------------------------

    def _yaml(self, ruta: Path) -> dict | None:
        if not ruta.is_file():
            return None
        return self.yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}

    def _sha(self, ruta: Path) -> str | None:
        return hashlib.sha256(ruta.read_bytes()).hexdigest() if ruta.is_file() else None

    def _marca(self) -> dict | None:
        if not MARCA.is_file():
            return None
        try:
            return json.loads(MARCA.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"instancia": "(ilegible)"}

    def _estado_s6(self, servicio: str) -> str:
        r = subprocess.run([S6_SVSTAT, str(SERVICIOS / servicio)], capture_output=True, text=True)
        return (r.stdout or r.stderr).strip()

    def _pid_s6(self, servicio: str) -> int | None:
        m = re.search(r"^up \(pid (\d+)", self._estado_s6(servicio))
        return int(m.group(1)) if m else None

    def _estado_gateway(self, carpeta: Path) -> dict:
        ruta = carpeta / "gateway_state.json"
        try:
            return json.loads(ruta.read_text(encoding="utf-8")) if ruta.is_file() else {}
        except json.JSONDecodeError:
            return {}

    def _variables(self, nombre: str) -> dict[str, str]:
        ruta = PROFILES / nombre / VARIABLES_DEL_PROFILE
        return leer_variables_de_texto(ruta.read_text(encoding="utf-8")) if ruta.is_file() else {}

    def _hermes(self, *args: str) -> None:
        r = subprocess.run([HERMES, *args], capture_output=True, text=True, env={**os.environ, "HOME": str(DATOS)})
        if r.returncode != 0:
            salida = (r.stdout + r.stderr).strip()[-500:]
            for por_profile in self.valores.values():
                for valor in por_profile.values():
                    salida = salida.replace(valor, "***")
            raise Freno(f"hermes {' '.join(args[:3])} fallo: {salida}")

    # --- preparar ------------------------------------------------------------------------

    def preparar(self) -> None:
        """Renderiza el config de cada profile en la copia y valida lo declarado."""
        for nombre, p in self.plan["profiles"].items():
            staged = Path(p["staged"])
            dist = self._yaml(staged / "distribution.yaml") or {}
            if dist.get("name") != nombre or dist.get("author") != AUTOR:
                raise Freno(f"la distribucion de {nombre} no se llama {nombre} o no es de {AUTOR}")
            requeridas = {e["name"] for e in dist.get("env_requires", []) if e.get("required")}
            faltan = sorted(requeridas - set(p["variables"]))
            if faltan:
                raise Freno(f"la instancia no le da a {nombre} las variables {', '.join(faltan)}")
            config = renderizar_config(self._yaml(staged / "config.yaml") or {}, p)
            (staged / "config.yaml").write_text(
                self.yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8"
            )
            self.esperado[nombre] = config
        if self.modo == "chequear":
            return  # chequear reporta un Hermes ajeno como diferencia; frenar es de plan y aplicar
        presentes = profiles_presentes(PROFILES)
        distribuciones = {n: self._yaml(PROFILES / n / "distribution.yaml") for n in self.plan["profiles"]}
        verificar_duenio(self.plan["stack"], self._marca(), presentes, list(self.plan["profiles"]), distribuciones)

    # --- aplicar -------------------------------------------------------------------------

    def aplicar(self, simular: bool) -> None:
        # La marca va primero: si aplicar se corta a mitad de camino, el reintento reconoce como
        # propio el profile que este mismo script ya creo.
        if self._marca() is None:
            self.reporte.acciones.append(f"marcar este Hermes como de la instancia {self.plan['stack']}")
            if not simular:
                tmp = MARCA.with_suffix(".tmp")
                tmp.write_text(json.dumps({
                    "instancia": self.plan["stack"],
                    "escrita": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "por": "scripts/hermes_instancia.py",
                }) + "\n", encoding="utf-8")
                tmp.replace(MARCA)
        for nombre, p in self.plan["profiles"].items():
            self._aplicar_profile(nombre, p, simular)

    def _aplicar_profile(self, nombre: str, p: dict, simular: bool) -> None:
        carpeta = PROFILES / nombre
        staged = Path(p["staged"])
        acciones = self.reporte.acciones
        if not carpeta.is_dir():
            acciones.append(f"crear el profile {nombre} (registra su servicio en s6, apagado)")
            if not simular:
                self._hermes("profile", "create", nombre, "--no-alias", "--no-skills",
                             "--description", f"Instalado por {AUTOR}")
        vivo = self._yaml(carpeta / "config.yaml") or {}
        api_vivo = ((vivo.get("platforms") or {}).get("api_server") or {})
        api_esperado = self.esperado[nombre]["platforms"]["api_server"]
        cambia_escucha = carpeta.is_dir() and any(api_vivo.get(k) != api_esperado.get(k) for k in ("enabled", "host", "port"))
        soul_igual = self._sha(carpeta / "SOUL.md") == self._sha(staged / "SOUL.md")
        config_igual = not diferencias_de_config(vivo, self.esperado[nombre])
        dist_nuestra = es_nuestro(self._yaml(carpeta / "distribution.yaml"), nombre)
        if not (soul_igual and config_igual and dist_nuestra):
            que = [x for x, igual in (("SOUL.md", soul_igual), ("config.yaml", config_igual), ("distribution.yaml", dist_nuestra)) if not igual]
            acciones.append(f"instalar la distribucion en {nombre} ({', '.join(que)}): el agente la usa desde el pedido siguiente")
            if not simular:
                self._hermes("profile", "install", str(staged), "--name", nombre, "--force", "-y")
                if self._sha(carpeta / "SOUL.md") != self._sha(staged / "SOUL.md"):
                    raise Freno(f"despues de instalar, el SOUL.md de {nombre} no coincide con Git")
                if diferencias_de_config(self._yaml(carpeta / "config.yaml") or {}, self.esperado[nombre]):
                    raise Freno(f"despues de instalar, el config.yaml de {nombre} no coincide con lo declarado")
        deseadas = self.valores.get(nombre, {})
        ruta_vars = carpeta / VARIABLES_DEL_PROFILE
        actual = ruta_vars.read_text(encoding="utf-8") if ruta_vars.is_file() else ""
        if simular:
            vivas = leer_variables_de_texto(actual)
            cambian = sorted(v for v in p["variables"] if huella(vivas.get(v, "")) != self.huellas.get(nombre, {}).get(v))
        else:
            nuevo, cambian = fusionar_variables(actual, deseadas)
            if cambian:
                tmp = carpeta / (VARIABLES_DEL_PROFILE + ".tmp")
                tmp.write_text(nuevo, encoding="utf-8")
                os.chmod(tmp, 0o600)
                tmp.replace(ruta_vars)
        if cambian:
            acciones.append(f"poner {', '.join(cambian)} en las variables de {nombre} (los valores no se muestran)")
        servicio = f"gateway-{nombre}"
        arriba = self._estado_s6(servicio).startswith("up")
        if not arriba or self._estado_gateway(carpeta).get("desired_state") != "running":
            acciones.append(f"arrancar el gateway de {nombre}")
            if not simular:
                self._hermes("--profile", nombre, "gateway", "start")
                self._esperar_gateway(nombre)
            return
        # El SOUL y el config.yaml se leen en cada pedido; el puerto y la clave del API server,
        # solo al arrancar el gateway (las dos cosas medidas el 2026-10-14). Si el API server no
        # responde con el puerto y la clave actuales, hay que reiniciar ese gateway.
        if simular:
            if cambia_escucha or "API_SERVER_KEY" in cambian:
                acciones.append(
                    f"reiniciar el gateway de {nombre}, porque cambia su puerto o su clave (solo con aplicar --reiniciar)"
                )
            return
        con, sin = self._probar_api(api_esperado["port"], self._variables(nombre).get("API_SERVER_KEY", ""))
        if (con, sin) == (200, 401):
            return
        if self.plan.get("reiniciar"):
            acciones.append(f"reiniciar el gateway de {nombre}: no respondia con el puerto y la clave actuales ({con} y {sin})")
            pid_anterior = self._pid_s6(servicio)
            self._hermes("--profile", nombre, "gateway", "restart")
            self._esperar_gateway(nombre, pid_anterior)
        else:
            self.reporte.diferencia(
                f"el gateway de {nombre} sigue con el puerto o la clave de antes: reiniciarlo con aplicar --reiniciar"
            )

    def _esperar_gateway(self, nombre: str, pid_anterior: int | None = None, segundos: int = 150) -> None:
        # Se prueba por presencia: s6 tiene arriba un proceso que no es el de antes, ese proceso
        # escribio "running" en gateway_state.json, y el API server responde 200 con la clave y 401
        # sin ella. El archivo solo no alcanza: despues de un reinicio conserva lo que escribio el
        # proceso anterior. El API solo tampoco: atiende antes de que el gateway pase de "starting"
        # a "running" (las dos cosas medidas el 2026-10-14).
        servicio = f"gateway-{nombre}"
        puerto = self.esperado[nombre]["platforms"]["api_server"]["port"]
        limite = time.monotonic() + segundos
        codigos: tuple[int, int] | None = None
        estado: dict = {}
        while time.monotonic() < limite:
            pid = self._pid_s6(servicio)
            if pid is not None and pid != pid_anterior:
                estado = self._estado_gateway(PROFILES / nombre)
                codigos = self._probar_api(puerto, self._variables(nombre).get("API_SERVER_KEY", ""))
                if codigos == (200, 401) and estado.get("pid") == pid and estado.get("gateway_state") == "running":
                    return
            time.sleep(2)
        estado = estado or self._estado_gateway(PROFILES / nombre)
        api = ((estado.get("platforms") or {}).get("api_server") or {}).get("state")
        sonda = f"{codigos[0]} con la clave y {codigos[1]} sin ella" if codigos else "sin probar, s6 no mostro un proceso nuevo"
        raise Freno(
            f"el gateway de {nombre} no quedo atendiendo en {segundos} s (s6: {self._estado_s6(servicio) or 'sin servicio'}; "
            f"gateway_state.json: {estado.get('gateway_state')} del pid {estado.get('pid')}, api_server {api}; API: {sonda})"
        )

    # --- chequear ------------------------------------------------------------------------

    def chequear(self) -> None:
        r = self.reporte
        marca = self._marca()
        if marca is None:
            r.diferencia("falta la marca de la instancia en /opt/data")
        elif marca.get("instancia") == self.plan["stack"]:
            r.ok(f"marca de la instancia {self.plan['stack']}")
        else:
            r.freno(f"este Hermes tiene la marca de la instancia '{marca.get('instancia')}'")
        presentes = profiles_presentes(PROFILES)
        declarados = list(self.plan["profiles"])
        for sobra in sorted(set(presentes) - set(declarados)):
            r.diferencia(f"sobra el profile {sobra}: no esta en la declaracion")
        for nombre, p in self.plan["profiles"].items():
            if nombre not in presentes:
                r.diferencia(f"falta el profile {nombre}")
                continue
            self._chequear_profile(nombre, p)
        default = self._estado_s6("gateway-default")
        raiz = self._estado_gateway(DATOS)
        if default.startswith("up") or "running" in (raiz.get("gateway_state"), raiz.get("desired_state")):
            r.diferencia("el gateway del profile default esta corriendo")
        else:
            r.ok("el profile default no corre")
        panel = self._estado_s6("dashboard")
        if panel.startswith("up") or os.environ.get("HERMES_DASHBOARD", "").strip().lower() in VERDADERO:
            r.diferencia("el panel de Hermes esta prendido")
        else:
            r.ok("el panel esta apagado")
        if os.environ.get("GATEWAY_MULTIPLEX_PROFILES", "").strip().lower() in VERDADERO:
            r.diferencia("GATEWAY_MULTIPLEX_PROFILES esta prendido")
        else:
            r.ok("sin multiplex de profiles")

    def _chequear_profile(self, nombre: str, p: dict) -> None:
        r = self.reporte
        carpeta = PROFILES / nombre
        staged = Path(p["staged"])
        if es_nuestro(self._yaml(carpeta / "distribution.yaml"), nombre):
            r.ok(f"{nombre}: lo instalo este script")
        else:
            r.diferencia(f"{nombre}: no tiene la distribucion de {AUTOR}")
        sha_vivo, sha_git = self._sha(carpeta / "SOUL.md"), self._sha(staged / "SOUL.md")
        if sha_vivo == sha_git:
            r.ok(f"{nombre}: SOUL.md igual a Git ({sha_git[:12]})")
        else:
            r.diferencia(f"{nombre}: SOUL.md distinto de Git ({(sha_vivo or 'sin archivo')[:12]} contra {sha_git[:12]})")
        vivo = self._yaml(carpeta / "config.yaml") or {}
        difs = diferencias_de_config(vivo, self.esperado[nombre])
        if difs:
            r.diferencia(f"{nombre}: config.yaml distinto de lo declarado en {', '.join(difs)}")
        else:
            r.ok(f"{nombre}: config.yaml igual a lo declarado")
        herramientas = self._herramientas_efectivas(carpeta, vivo)
        if herramientas is None:
            r.diferencia(f"{nombre}: no se pudo calcular sus herramientas con Hermes")
        elif herramientas:
            r.diferencia(f"{nombre}: tiene herramientas en el API server: {', '.join(herramientas)}")
        else:
            r.ok(f"{nombre}: cero herramientas en el API server")
        vivas = self._variables(nombre)
        for var in p["variables"]:
            if var not in vivas:
                r.diferencia(f"{nombre}: falta {var} en sus variables")
            elif huella(vivas[var]) != self.huellas.get(nombre, {}).get(var):
                r.diferencia(f"{nombre}: {var} no coincide con los secretos de la instancia")
            else:
                r.ok(f"{nombre}: {var} coincide con los secretos de la instancia")
        de_mas = sorted(set(vivas) - set(p["variables"]))
        if de_mas:
            r.diferencia(f"{nombre}: variables que la instancia no declara: {', '.join(de_mas)}")
        s6 = self._estado_s6(f"gateway-{nombre}")
        estado = self._estado_gateway(carpeta)
        plataformas = estado.get("platforms") or {}
        if not s6.startswith("up") or estado.get("gateway_state") != "running" or estado.get("desired_state") != "running":
            r.diferencia(f"{nombre}: el gateway no esta corriendo ({s6.split(' (')[0] or 'sin servicio'})")
            return
        r.ok(f"{nombre}: el gateway corre")
        otras = sorted(set(plataformas) - {"api_server"})
        if otras:
            r.diferencia(f"{nombre}: el gateway tiene otras plataformas: {', '.join(otras)}")
        if (plataformas.get("api_server") or {}).get("state") != "connected":
            r.diferencia(f"{nombre}: el API server no esta conectado")
        con, sin = self._probar_api(p["puerto"], vivas.get("API_SERVER_KEY", ""))
        if (con, sin) == (200, 401):
            r.ok(f"{nombre}: el API server del puerto {p['puerto']} responde 200 con la clave y 401 sin ella")
        else:
            r.diferencia(
                f"{nombre}: el API server del puerto {p['puerto']} responde {con} con la clave y {sin} sin ella "
                "(esperado 200 y 401; si cambio la clave o el puerto, falta aplicar --reiniciar)"
            )

    def _herramientas_efectivas(self, carpeta: Path, config: dict) -> list[str] | None:
        try:
            sys.path.insert(0, "/opt/hermes")
            from unittest import mock

            from hermes_cli import tools_config  # type: ignore

            claves: set[str] = set()
            for home in (carpeta, DATOS):
                cache = home / "cache" / "plugin_toolset_keys.json"
                if cache.is_file():
                    crudo = json.loads(cache.read_text(encoding="utf-8"))
                    claves |= set(crudo.get("toolset_keys", []) if isinstance(crudo, dict) else crudo)
            if not claves:
                claves = set(tools_config._get_plugin_toolset_keys())
            with mock.patch.object(tools_config, "_get_plugin_toolset_keys", return_value=claves):
                return sorted(tools_config._get_platform_tools(config, "api_server"))
        except Exception:
            return None

    def _probar_api(self, puerto: int, clave: str) -> tuple[int, int]:
        def pedir(con_clave: bool) -> int:
            pedido = urllib.request.Request(f"http://127.0.0.1:{puerto}/v1/models")
            if con_clave and clave:
                pedido.add_header("Authorization", f"Bearer {clave}")
            try:
                with urllib.request.urlopen(pedido, timeout=10) as respuesta:
                    return respuesta.status
            except urllib.error.HTTPError as exc:
                return exc.code
            except OSError:
                return 0

        return pedir(True), pedir(False)


def dentro(ruta_plan: Path) -> int:
    plan = json.loads(ruta_plan.read_text(encoding="utf-8"))
    entrada = json.loads(sys.stdin.read() or "{}")
    d = Dentro(plan, entrada)
    try:
        d.preparar()
        if d.modo in ("plan", "aplicar"):
            d.aplicar(simular=d.modo == "plan")
        if d.modo in ("aplicar", "chequear"):
            d.chequear()
    except Freno as exc:
        d.reporte.freno(str(exc))
    print(json.dumps({"hallazgos": d.reporte.hallazgos, "acciones": d.reporte.acciones}))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--dentro"]:
        return dentro(Path(argv[1]))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("modo", choices=("plan", "aplicar", "chequear"))
    parser.add_argument("--instancia", type=Path, required=True, help="el hermes.toml de la instancia")
    parser.add_argument("--contenedor", help="el contenedor de Hermes, si no se busca por el servicio del stack")
    parser.add_argument("--reiniciar", action="store_true", help="con aplicar: reinicia un gateway si cambia su puerto")
    parser.add_argument("--declaracion", type=Path, default=DECLARACION, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        return correr(args.modo, args.instancia, args.contenedor, args.reiniciar, args.declaracion)
    except Freno as exc:
        print(f"FRENO: {exc}. No se toco nada.", file=sys.stderr)
        return SALIDA_FRENO


if __name__ == "__main__":
    sys.exit(main())
