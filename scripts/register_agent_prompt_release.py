#!/usr/bin/env python3
"""Registra la composicion del prompt del agente, tal como esta donde vive.

Corre DENTRO de infra_hermes, que es el unico lugar desde donde se ven los
artefactos que el agente lee: el bridge no los tiene (su imagen no trae
`profiles/` y su unico volumen es su propia data), y derivarlos de Git seria
mentira --- el SOUL se edita a mano en el VPS, y al 2026-09-27 habia tres copias
fechadas de septiembre al lado del archivo vivo.

Que hace, en una corrida:
    1. hashea cada artefacto del perfil y lee su mtime
    2. arma el digest del release: sha256 del manifiesto canonico
    3. llama a register_agent_prompt_release_v1

Es idempotente por diseno: si nada cambio, la RPC devuelve `unchanged` y no
escribe. Por eso puede colgarse del cron del perfil y correr seguido.

QUE SE GUARDA Y QUE NO. Del SOUL se guarda el TEXTO COMPLETO: es el prompt, y es
lo que hace falta para entender un feedback viejo. De los demas artefactos se
guarda SOLO EL HASH, nunca el contenido, porque `config.yaml` puede llevar
credenciales del perfil. Este script no imprime el contenido de ningun artefacto
salvo, con --dry-run, la cantidad de bytes del SOUL.

Uso, adentro del contenedor:
    python3 scripts/register_agent_prompt_release.py --dry-run
    python3 scripts/register_agent_prompt_release.py
    python3 scripts/register_agent_prompt_release.py --json

Credenciales: primero PostgREST (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY); si
no estan, la Management API (SUPABASE_PROJECT_REF + SUPABASE_ACCESS_TOKEN), que
es la que el contenedor ya tiene. Se leen del entorno y no se imprimen nunca.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

PERFIL_POR_DEFECTO = Path("/opt/data/profiles/agente-comercial")
TENANT_POR_DEFECTO = "lancemos"
SCOPE_POR_DEFECTO = "psicologajohanna-agent-bot-19"
NOMBRE_POR_DEFECTO = "agente-comercial"

# El orden no importa para el digest (el manifiesto se ordena), pero si para el
# reporte. `.skills_prompt_snapshot.json` es el snapshot que Hermes escribe con
# las descripciones de las skills: una description es prompt, asi que entra.
ARTEFACTOS_POR_DEFECTO = ("SOUL.md", "config.yaml", ".skills_prompt_snapshot.json")

# El archivo cuyo texto completo se guarda. Los demas, solo por hash.
ARTEFACTO_CON_TEXTO = "SOUL.md"

TIMEOUT = 30
MAX_SOUL = 200_000


def _sha256(ruta: Path) -> str:
    resumen = hashlib.sha256()
    with ruta.open("rb") as f:
        for bloque in iter(lambda: f.read(65536), b""):
            resumen.update(bloque)
    return resumen.hexdigest()


def _utc(marca: float) -> str:
    return datetime.fromtimestamp(marca, tz=UTC).isoformat()


def observar(directorio: Path, nombres: tuple[str, ...]) -> dict:
    """Mira los artefactos y devuelve el manifiesto, sin imprimir contenido."""
    if not directorio.is_dir():
        raise SystemExit(f"no existe el directorio del perfil: {directorio}")

    artefactos: dict[str, dict] = {}
    faltantes: list[str] = []
    mas_nuevo = 0.0

    for nombre in nombres:
        ruta = directorio / nombre
        if ruta.is_symlink():
            raise SystemExit(f"artefacto que es symlink, no se sigue: {nombre}")
        if not ruta.is_file():
            faltantes.append(nombre)
            continue
        estado = ruta.stat()
        artefactos[nombre] = {
            "sha256": _sha256(ruta),
            "bytes": estado.st_size,
            "modified_at": _utc(estado.st_mtime),
        }
        mas_nuevo = max(mas_nuevo, estado.st_mtime)

    if not artefactos:
        raise SystemExit(f"ningun artefacto encontrado en {directorio}")

    ruta_texto = directorio / ARTEFACTO_CON_TEXTO
    if ARTEFACTO_CON_TEXTO not in artefactos:
        raise SystemExit(f"falta {ARTEFACTO_CON_TEXTO}: sin el prompt no hay release")
    texto = ruta_texto.read_text(encoding="utf-8")
    if not texto.strip():
        raise SystemExit(f"{ARTEFACTO_CON_TEXTO} esta vacio")
    if len(texto) > MAX_SOUL:
        raise SystemExit(
            f"{ARTEFACTO_CON_TEXTO} tiene {len(texto)} caracteres y el limite es {MAX_SOUL}"
        )

    # El digest del release: sha256 del manifiesto canonico. Depende solo de las
    # rutas y sus hashes --- no del mtime, para que tocar un archivo sin
    # cambiarlo no invente un release nuevo.
    canonico = json.dumps(
        {nombre: datos["sha256"] for nombre, datos in sorted(artefactos.items())},
        sort_keys=True,
        separators=(",", ":"),
    )

    return {
        "artifacts": artefactos,
        "missing": faltantes,
        "artifacts_modified_at": _utc(mas_nuevo),
        "release_digest": hashlib.sha256(canonico.encode("utf-8")).hexdigest(),
        "soul_text": texto,
    }


# --- como se le habla a Supabase ---------------------------------------------


def _pedir(url: str, cuerpo: bytes, cabeceras: dict[str, str]) -> object:
    peticion = urllib.request.Request(url, data=cuerpo, headers=cabeceras, method="POST")
    try:
        with urllib.request.urlopen(peticion, timeout=TIMEOUT) as respuesta:
            return json.load(respuesta)
    except urllib.error.HTTPError as error:
        detalle = error.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"Supabase respondio {error.code}: {detalle}") from None


def por_postgrest(url_base: str, clave: str, carga: dict) -> object:
    return _pedir(
        f"{url_base.rstrip('/')}/rest/v1/rpc/register_agent_prompt_release_v1",
        json.dumps(
            {
                "p_tenant_ref": carga["tenant_ref"],
                "p_scope_ref": carga["scope_ref"],
                "p_profile_name": carga["profile_name"],
                "p_release_digest": carga["release_digest"],
                "p_artifacts": carga["artifacts"],
                "p_artifacts_modified_at": carga["artifacts_modified_at"],
                "p_soul_text": carga["soul_text"],
                "p_observed_at": carga["observed_at"],
                "p_registered_by": carga["registered_by"],
            }
        ).encode("utf-8"),
        {
            "apikey": clave,
            "Authorization": f"Bearer {clave}",
            "Content-Type": "application/json",
        },
    )


def por_management(ref: str, token: str, carga: dict) -> object:
    """La carga viaja como UN literal dollar-quoted y el SQL la desarma.

    Interpolar el texto del SOUL en SQL a mano seria pedir un escape roto: el
    prompt tiene comillas, saltos y acentos. El literal se cita con una etiqueta
    aleatoria y se verifica que no aparezca en el contenido.
    """
    texto = json.dumps(carga, ensure_ascii=False)
    for _ in range(8):
        etiqueta = f"prov{secrets.token_hex(8)}"
        if f"${etiqueta}$" not in texto:
            break
    else:  # pragma: no cover - ocho colisiones seguidas no pasan
        raise SystemExit("no se pudo elegir una etiqueta de cita segura")

    sql = f"""
with carga as (select ${etiqueta}${texto}${etiqueta}$::jsonb as p)
select public.register_agent_prompt_release_v1(
    p->>'tenant_ref',
    p->>'scope_ref',
    p->>'profile_name',
    p->>'release_digest',
    p->'artifacts',
    (p->>'artifacts_modified_at')::timestamptz,
    p->>'soul_text',
    (p->>'observed_at')::timestamptz,
    p->>'registered_by'
) as resultado
from carga
"""
    return _pedir(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        json.dumps({"query": sql}).encode("utf-8"),
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )


def registrar(carga: dict) -> tuple[str, object]:
    url = os.getenv("SUPABASE_URL")
    clave = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if url and clave:
        return "postgrest", por_postgrest(url, clave, carga)

    ref = os.getenv("SUPABASE_PROJECT_REF")
    token = os.getenv("SUPABASE_ACCESS_TOKEN")
    if ref and token:
        return "management", por_management(ref, token, carga)

    raise SystemExit(
        "faltan credenciales: se necesita SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY, "
        "o SUPABASE_PROJECT_REF + SUPABASE_ACCESS_TOKEN"
    )


def _desanidar(respuesta: object) -> dict:
    """La Management API devuelve filas; PostgREST devuelve el jsonb pelado."""
    if isinstance(respuesta, dict):
        return respuesta
    if isinstance(respuesta, list) and respuesta:
        primera = respuesta[0]
        if isinstance(primera, dict):
            return primera.get("resultado") or primera
    return {"outcome": "unknown", "raw": respuesta}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Registra la composicion del prompt del agente en Supabase."
    )
    ap.add_argument("--profile-dir", type=Path, default=PERFIL_POR_DEFECTO)
    ap.add_argument("--tenant", default=TENANT_POR_DEFECTO)
    ap.add_argument("--scope", default=SCOPE_POR_DEFECTO)
    ap.add_argument("--profile-name", default=NOMBRE_POR_DEFECTO)
    ap.add_argument(
        "--artifact",
        action="append",
        default=None,
        help="artefacto a observar; se puede repetir. Por defecto: "
        + ", ".join(ARTEFACTOS_POR_DEFECTO),
    )
    ap.add_argument("--registered-by", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="calcula el digest y no escribe nada")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    nombres = tuple(args.artifact) if args.artifact else ARTEFACTOS_POR_DEFECTO
    vista = observar(args.profile_dir, nombres)

    carga = {
        "tenant_ref": args.tenant,
        "scope_ref": args.scope,
        "profile_name": args.profile_name,
        "release_digest": vista["release_digest"],
        "artifacts": vista["artifacts"],
        "artifacts_modified_at": vista["artifacts_modified_at"],
        "soul_text": vista["soul_text"],
        "observed_at": datetime.now(UTC).isoformat(),
        "registered_by": args.registered_by
        or f"register_agent_prompt_release@{os.uname().nodename}",
    }

    if args.dry_run:
        informe = {
            "modo": "dry-run",
            "release_digest": vista["release_digest"],
            "artifacts_modified_at": vista["artifacts_modified_at"],
            "artifacts": {
                nombre: {"sha256": datos["sha256"], "bytes": datos["bytes"],
                         "modified_at": datos["modified_at"]}
                for nombre, datos in vista["artifacts"].items()
            },
            "missing": vista["missing"],
            "soul_bytes": len(vista["soul_text"]),
        }
        print(json.dumps(informe, indent=2) if args.json else _humano(informe))
        return 0

    via, respuesta = registrar(carga)
    resultado = _desanidar(respuesta)
    resultado["via"] = via
    resultado["release_digest"] = resultado.get("release_digest") or vista["release_digest"]
    resultado["missing"] = vista["missing"]

    if args.json:
        print(json.dumps(resultado, indent=2, default=str))
    else:
        print(_humano(resultado))
    return 0 if resultado.get("outcome") in {"registered", "unchanged"} else 1


def _humano(datos: dict) -> str:
    lineas = ["PROCEDENCIA DEL PROMPT DEL AGENTE"]
    if datos.get("modo") == "dry-run":
        lineas.append("  modo: dry-run, no se escribio nada")
    else:
        lineas.append(f"  resultado: {datos.get('outcome')}")
        if datos.get("release_ordinal") is not None:
            lineas.append(f"  version del release: {datos.get('release_ordinal')}")
        lineas.append(f"  via: {datos.get('via')}")
    lineas.append(f"  digest: {datos.get('release_digest')}")
    if datos.get("artifacts_modified_at"):
        lineas.append(f"  ultimo cambio de artefactos: {datos['artifacts_modified_at']}")
    for nombre, detalle in (datos.get("artifacts") or {}).items():
        lineas.append(
            f"    {nombre:<32} {detalle['sha256'][:16]}… "
            f"{detalle['bytes']} bytes  {detalle['modified_at']}"
        )
    if datos.get("soul_bytes"):
        lineas.append(f"  SOUL: {datos['soul_bytes']} caracteres")
    if datos.get("missing"):
        lineas.append(f"  NO ESTABAN: {', '.join(datos['missing'])}")
    return "\n".join(lineas)


if __name__ == "__main__":
    raise SystemExit(main())
