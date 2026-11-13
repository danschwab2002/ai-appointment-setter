"""Los numeros del tablero de resultados, en el bridge (E-507, resultados/v1).

``GET /internal/automation-expert/v1/resultados?desde=AAAA-MM-DD&hasta=AAAA-MM-DD``
contesta lo que hoy arma el tablero del OS de Dan (``tablero_recuperador.py``) con la
misma regla (venta_recuperada_v1) y la misma forma de filas, para que una instancia
nueva tenga sus numeros sin instalar nada aparte:

- ventas recuperadas, partidas en directas (via link) y atribuibles (via caso o
  identidad), con la comision PRODUCER en USD de cada una;
- por flujo de negocio y ejecutor: envios aceptados, ventas y por que via;
- las compras aprobadas de la ventana por via, los dias y las campanas;
- el costo de Meta por dia (``pricing_analytics`` de la cuenta de WhatsApp), con
  cache de una hora; sin la cuenta configurada, va en ``faltan``.

Lo que no esta en la base de la instancia (conversaciones de Chatwoot, costo del
modelo de Hermes) va en ``faltan``: el tablero lo sigue leyendo por su lado.

Va detras de ``AUTOMATION_EXPERT_RESULTS_ENABLED`` (apagado por defecto). Apagado,
la ruta contesta 404 como si no existiera. Usa el mismo token de lectura que el
resto de la lectura del Automation Expert. Nada de una persona sale: las ventas van
por un pedazo de su id.

Vive aparte de ``app.py`` a proposito: la app solo llama a :func:`register`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, status

from bridge.meta_templates import DEFAULT_GRAPH_VERSION, GRAPH_ORIGIN

logger = logging.getLogger(__name__)

VERSION = "resultados/v1"
RPC = "/rest/v1/rpc/read_results_v1"
ROUTE = "/internal/automation-expert/v1/resultados"
ENV_ENABLED = "AUTOMATION_EXPERT_RESULTS_ENABLED"
ENV_TEST_PREFIXES = "AUTOMATION_EXPERT_RESULTS_TEST_PREFIXES"

# La vara de venta_recuperada_v1 (los mismos valores que el OS).
VENTANA_ATRIBUCION_DIAS = 7
PISO_ATRIBUCION_MIN = 15
DIAS_POR_DEFECTO = 30
MAX_DIAS = 400
META_CACHE_SECONDS = 3600
MAX_META_BYTES = 2 * 1024 * 1024
FALTAN_SIEMPRE = ("conversaciones", "costo_modelo")

TIPO_POR_VIA = {"link": "directa", "caso": "atribuible", "identidad": "atribuible"}
_DIA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PREFIJO = re.compile(r"^[0-9a-f-]{4,36}$")
_WABA = re.compile(r"^[0-9]{6,32}$")
_GRAPH_VERSION = re.compile(r"^v[0-9]{1,3}\.[0-9]{1,3}$")


class ResultsRejected(ValueError):
    """Un pedido mal formado: 422 con el codigo, nunca con el valor."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# La configuracion.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResultsConfig:
    enabled: bool = False
    test_prefixes: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ResultsConfig:
        enabled = env.get(ENV_ENABLED, "false").strip().lower() == "true"
        prefixes = tuple(p.strip().lower() for p in env.get(ENV_TEST_PREFIXES, "").split(",") if p.strip())
        malos = [p for p in prefixes if not _PREFIJO.fullmatch(p)]
        if enabled and malos:
            # Un prefijo mal escrito haria contar una prueba como venta: no arranca.
            raise ValueError(f"{ENV_TEST_PREFIXES} lleva {len(malos)} valor(es) que no son un prefijo de id")
        return cls(enabled=enabled, test_prefixes=prefixes)


# ---------------------------------------------------------------------------
# La ventana.
# ---------------------------------------------------------------------------

def _dia(texto: str, campo: str) -> date:
    if not _DIA.fullmatch(texto):
        raise ResultsRejected(f"{campo}_invalid")
    try:
        return date.fromisoformat(texto)
    except ValueError:
        raise ResultsRejected(f"{campo}_invalid") from None


def parse_window(desde: str | None, hasta: str | None, *, tz: str, now: datetime) -> tuple[datetime, datetime]:
    """[desde, hasta) en UTC. desde = las 00:00 de ese dia en la zona de la instancia;
    hasta incluye su dia entero y se recorta a ahora. Sin desde, los ultimos 30 dias."""
    zona = ZoneInfo(tz)
    if hasta:
        fin_dia = _dia(hasta, "hasta") + timedelta(days=1)
        fin = min(datetime(fin_dia.year, fin_dia.month, fin_dia.day, tzinfo=zona).astimezone(UTC), now)
    else:
        fin = now
    if desde:
        d = _dia(desde, "desde")
        inicio = datetime(d.year, d.month, d.day, tzinfo=zona).astimezone(UTC)
    else:
        inicio = fin - timedelta(days=DIAS_POR_DEFECTO)
    if inicio >= fin:
        raise ResultsRejected("window_empty")
    if fin - inicio > timedelta(days=MAX_DIAS):
        raise ResultsRejected("window_too_long")
    return inicio, fin


def _iso(momento: datetime) -> str:
    return momento.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# La base: read_results_v1.
# ---------------------------------------------------------------------------

def rpc_payload(desde: datetime, hasta: datetime, *, tz: str, test_prefixes: Sequence[str]) -> dict[str, object]:
    return {
        "p_desde": _iso(desde),
        "p_hasta": _iso(hasta),
        "p_tz": tz,
        "p_pruebas": list(test_prefixes),
        "p_ventana_dias": VENTANA_ATRIBUCION_DIAS,
        "p_piso_min": PISO_ATRIBUCION_MIN,
    }


async def read_results_v1(supabase: Any, payload: Mapping[str, object]) -> dict[str, Any]:
    """Llama a la RPC por el mismo camino que el resto de las lecturas del AE."""
    from bridge.supabase import SupabaseError, _automation_expert_value

    response = await supabase._request("POST", RPC, content=json.dumps(payload, separators=(",", ":")))
    value = _automation_expert_value(response, operation="read_results")
    if not isinstance(value, dict):
        raise SupabaseError("read_results_invalid_shape")
    return value


# ---------------------------------------------------------------------------
# Meta: el costo por dia.
# ---------------------------------------------------------------------------

def costo_por_dia(respuesta: Mapping[str, Any], tz: str) -> list[dict[str, object]]:
    """La analitica de precios de Meta agregada por dia de la zona de la instancia.

    Cobradas = pricing_type REGULAR (con su costo, partidas en marketing y utilidad);
    el resto (FREE_CUSTOMER_SERVICE, FREE_ENTRY_POINT) va en gratis. Mismo calculo que
    ``costo_por_dia`` de meta_salud.py del OS."""
    zona = ZoneInfo(tz)
    dias: dict[str, dict[str, Any]] = {}
    analitica = respuesta.get("pricing_analytics") or {}
    for bloque in analitica.get("data", []) if isinstance(analitica, Mapping) else []:
        for q in bloque.get("data_points", []) if isinstance(bloque, Mapping) else []:
            if not isinstance(q, Mapping) or not isinstance(q.get("start"), int):
                continue
            dia = datetime.fromtimestamp(q["start"], zona).strftime("%Y-%m-%d")
            r = dias.setdefault(dia, {"fecha": dia, "plantillas_cobradas": 0, "costo_usd": 0.0,
                                      "marketing": 0, "utilidad": 0, "gratis": 0})
            volumen = q.get("volume", 0) if isinstance(q.get("volume"), int) else 0
            if q.get("pricing_type") == "REGULAR":
                r["plantillas_cobradas"] += volumen
                costo = q.get("cost", 0.0)
                r["costo_usd"] += float(costo) if isinstance(costo, (int, float)) else 0.0
                if q.get("pricing_category") == "MARKETING":
                    r["marketing"] += volumen
                elif q.get("pricing_category") == "UTILITY":
                    r["utilidad"] += volumen
            else:
                r["gratis"] += volumen
    for r in dias.values():
        r["costo_usd"] = round(r["costo_usd"], 4)
    return [dias[k] for k in sorted(dias)]


@dataclass
class MetaPricing:
    """pricing_analytics de la cuenta de WhatsApp, con cache de una hora por ventana."""

    access_token: str
    waba_id: str
    graph_version: str = DEFAULT_GRAPH_VERSION
    transport: httpx.AsyncBaseTransport | None = None
    timeout_seconds: float = 20.0
    clock: Callable[[], float] = time.monotonic
    _cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.access_token or any(c.isspace() for c in self.access_token):
            raise ValueError("meta_pricing_access_token_invalid")
        if not _WABA.fullmatch(self.waba_id):
            raise ValueError("meta_pricing_waba_invalid")
        if not _GRAPH_VERSION.fullmatch(self.graph_version):
            raise ValueError("meta_pricing_graph_version_invalid")

    def __repr__(self) -> str:
        return f"MetaPricing(graph_version={self.graph_version!r})"

    async def pricing_analytics(self, desde: datetime, hasta: datetime) -> dict[str, Any]:
        clave = (_iso(desde)[:13], _iso(hasta)[:13])  # la hora: un pedido por hora y ventana
        guardado = self._cache.get(clave)
        if guardado is not None and self.clock() - guardado[0] < META_CACHE_SECONDS:
            return guardado[1]
        campo = (f"pricing_analytics.start({int(desde.timestamp())}).end({int(hasta.timestamp())})"
                 '.granularity(DAILY).dimensions(["PRICING_CATEGORY","PRICING_TYPE","COUNTRY"])')
        async with httpx.AsyncClient(base_url=GRAPH_ORIGIN, transport=self.transport,
                                     timeout=self.timeout_seconds, follow_redirects=False,
                                     headers={"Authorization": f"Bearer {self.access_token}"}) as http:
            response = await http.get(f"/{self.graph_version}/{self.waba_id}", params={"fields": campo})
        if response.status_code != 200:
            raise RuntimeError(f"meta_pricing_http_{response.status_code}")
        if len(response.content) > MAX_META_BYTES:
            raise RuntimeError("meta_pricing_too_large")
        try:
            cuerpo = response.json()
        except ValueError:
            raise RuntimeError("meta_pricing_invalid_json") from None
        if not isinstance(cuerpo, dict):
            raise RuntimeError("meta_pricing_invalid_shape")
        self._cache[clave] = (self.clock(), cuerpo)
        return cuerpo


# ---------------------------------------------------------------------------
# La respuesta.
# ---------------------------------------------------------------------------

def _usd(valor: object) -> float:
    return float(valor) if isinstance(valor, (int, float)) and not isinstance(valor, bool) else 0.0


def build_body(
    raw: Mapping[str, Any],
    *,
    instancia: str,
    zona: str,
    desde: datetime,
    hasta: datetime,
    generado: datetime,
    costo_meta: list[dict[str, object]] | None,
) -> dict[str, object]:
    """La respuesta de /resultados, con las filas en la forma de la foto del tablero."""
    ventas_rpc = [v for v in raw.get("ventas") or [] if isinstance(v, Mapping) and v.get("via") in TIPO_POR_VIA]
    ventas = [{
        "venta": v.get("venta"), "fecha": v.get("fecha"), "flujo": v.get("flujo"), "via": v.get("via"),
        "tipo": TIPO_POR_VIA[str(v.get("via"))], "horas_desde_toque": v.get("horas_desde_toque"),
        "pago": v.get("pago"), "pais": v.get("pais"), "moneda": v.get("moneda"), "precio": v.get("precio"),
        "neto_usd": v.get("neto_usd"),
    } for v in ventas_rpc]
    sin_monto = sum(1 for v in ventas if v["neto_usd"] is None)

    tot = {"directa": [0, 0.0], "atribuible": [0, 0.0]}
    por_flujo: dict[tuple[object, object], dict[str, list]] = {}
    for v in ventas_rpc:
        tipo = TIPO_POR_VIA[str(v.get("via"))]
        usd = _usd(v.get("neto_usd"))
        tot[tipo][0] += 1
        tot[tipo][1] += usd
        f = por_flujo.setdefault((v.get("flujo"), v.get("ejecutor")), {"directa": [0, 0.0], "atribuible": [0, 0.0]})
        f[tipo][0] += 1
        f[tipo][1] += usd

    flujos = []
    for f in raw.get("flujos") or []:
        if not isinstance(f, Mapping):
            continue
        c = por_flujo.get((f.get("flujo"), f.get("ejecutor")), {"directa": [0, 0.0], "atribuible": [0, 0.0]})
        flujos.append({
            "flujo": f.get("flujo"), "ejecutor": f.get("ejecutor"), "envios": f.get("aceptados"),
            "ventas": f.get("ventas"), "por_link": f.get("por_link"), "por_caso": f.get("por_caso"),
            "por_identidad": f.get("por_identidad"),
            "ventas_directas": c["directa"][0], "neto_directas_usd": round(c["directa"][1], 2),
            "ventas_atribuibles": c["atribuible"][0], "neto_atribuibles_usd": round(c["atribuible"][1], 2),
        })

    compras = [{"via": r.get("via"), "compras": r.get("n")} for r in raw.get("compras") or []
               if isinstance(r, Mapping) and r.get("via") != "prueba"]
    neto = round(sum(_usd(v["neto_usd"]) for v in ventas), 2)
    faltan = list(FALTAN_SIEMPRE)
    if costo_meta is None:
        faltan.append("costo_meta")
    if sin_monto:
        faltan.append("monto_de_alguna_venta")
    resumen = [
        {"metrica": "ventas_recuperadas", "valor": len(ventas)},
        {"metrica": "neto_productor_usd", "valor": neto},
        {"metrica": "costo_meta_usd",
         "valor": round(sum(_usd(r["costo_usd"]) for r in costo_meta), 2) if costo_meta is not None else None},
        {"metrica": "compras_aprobadas", "valor": sum(int(r["compras"] or 0) for r in compras)},
        {"metrica": "ventas_directas", "valor": tot["directa"][0]},
        {"metrica": "neto_directas_usd", "valor": round(tot["directa"][1], 2)},
        {"metrica": "ventas_atribuibles", "valor": tot["atribuible"][0]},
        {"metrica": "neto_atribuibles_usd", "valor": round(tot["atribuible"][1], 2)},
    ]
    return {
        "version": VERSION,
        "instancia": instancia,
        "zona": zona,
        "desde": _iso(desde),
        "hasta": _iso(hasta),
        "generado": generado.astimezone(UTC).isoformat(timespec="seconds"),
        "regla": "venta_recuperada_v1",
        "ventana_atribucion_dias": VENTANA_ATRIBUCION_DIAS,
        "piso_min": PISO_ATRIBUCION_MIN,
        "resumen": resumen,
        "flujos": flujos,
        "ventas": ventas,
        "compras": compras,
        "dias": [dict(d) for d in raw.get("dias") or [] if isinstance(d, Mapping)],
        "campanas": [dict(c) for c in raw.get("campanas") or [] if isinstance(c, Mapping)],
        "costo_meta": costo_meta or [],
        "faltan": faltan,
    }


# ---------------------------------------------------------------------------
# La ruta.
# ---------------------------------------------------------------------------

def register(
    app: FastAPI,
    *,
    supabase: Any,
    instance_ref: str,
    time_zone: str,
    require_token: Callable[[str | None], None],
    busy: Callable[[], None],
    slots: asyncio.Semaphore,
    failure: Callable[[Exception], HTTPException],
    meta_access_token: str | None,
    meta_waba_id: str | None,
    graph_version: str = DEFAULT_GRAPH_VERSION,
    env: Mapping[str, str] | None = None,
    meta: MetaPricing | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ResultsConfig:
    """Suma la ruta. Con el flag apagado contesta 404, como si no existiera."""
    config = ResultsConfig.from_env(os.environ if env is None else env)
    try:
        ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("results_time_zone_invalid") from None
    pricing = meta
    if pricing is None and config.enabled and meta_access_token and meta_waba_id:
        pricing = MetaPricing(access_token=meta_access_token, waba_id=meta_waba_id, graph_version=graph_version)

    @app.get(ROUTE)
    async def automation_expert_results(
        request: Request,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, object]:
        if not config.enabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
        require_token(authorization)
        try:
            desde, hasta = parse_window(request.query_params.get("desde"), request.query_params.get("hasta"),
                                        tz=time_zone, now=now())
        except ResultsRejected as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.code) from None
        busy()
        async with slots:
            try:
                raw = await read_results_v1(
                    supabase, rpc_payload(desde, hasta, tz=time_zone, test_prefixes=config.test_prefixes))
            except Exception as exc:  # noqa: BLE001 - el log lleva solo el tipo
                logger.warning("automation_expert_read_failed route=resultados error_type=%s", type(exc).__name__)
                raise failure(exc) from None
        costo: list[dict[str, object]] | None = None
        if pricing is not None:
            try:
                costo = costo_por_dia(await pricing.pricing_analytics(desde, hasta), time_zone)
            except Exception as exc:  # noqa: BLE001 - sin costo, la respuesta sale igual y lo dice
                logger.warning("automation_expert_results_meta_failed error_type=%s", type(exc).__name__)
        body = build_body(raw, instancia=instance_ref, zona=time_zone, desde=desde, hasta=hasta,
                          generado=now(), costo_meta=costo)
        logger.info("automation_expert_access route=resultados status=200")
        return body

    return config
