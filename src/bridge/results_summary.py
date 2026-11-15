"""El resumen de resultados para el Automation Expert (E-573, automation-expert-results-v1).

``GET /internal/automation-expert/v1/resultados/resumen`` contesta «como venimos» con
los mismos numeros de ``/resultados`` (resultados/v1, la regla venta_recuperada_v1),
pero cortos y listos para decir por Slack:

- la ventana en dias de la zona de la instancia (por defecto los ultimos 7, con hoy
  parcial), y el mismo largo de dias justo antes, para comparar;
- ventas recuperadas (por link nuestro y atribuibles), la comision del productor en
  USD, los envios aceptados de los flujos, las compras aprobadas y el costo de Meta;
- hasta 10 flujos ordenados por ventas, y cuantos quedaron afuera;
- ``lineas``: el resumen ya escrito en castellano, para que el agente no haga cuentas;
- ``gaps``: lo que la instancia no mide (conversaciones, costo del modelo, a veces el
  costo de Meta), que el agente nombra tal cual.

No sale ninguna venta suelta: ni su pedazo de id, ni el pais, ni el precio. La forma
lleva ``contract_version`` 1.x para que el plugin de lectura del AE la acepte.

Va detras de dos flags: ``AUTOMATION_EXPERT_RESULTS_ENABLED`` (la ruta de los numeros,
que declara tambien las pruebas que no cuentan) y ``AUTOMATION_EXPERT_RESULTS_SUMMARY_ENABLED``
(esta ruta). Con cualquiera de los dos apagado contesta 404, como si no existiera.
La registra :func:`bridge.results_read.register`: ``app.py`` no cambia.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Request, status

from bridge.results_read import (
    ResultsConfig,
    ResultsRejected,
    _dia,
    build_body,
    costo_por_dia,
    read_results_v1,
    rpc_payload,
)

logger = logging.getLogger(__name__)

CONTRACT = "automation-expert-results-v1"
CONTRACT_VERSION = "1.0.0"
ROUTE = "/internal/automation-expert/v1/resultados/resumen"
ENV_ENABLED = "AUTOMATION_EXPERT_RESULTS_SUMMARY_ENABLED"
DIAS_POR_DEFECTO = 7
MAX_DIAS = 90
MAX_FLUJOS = 10
META_TIMEOUT_SECONDS = 5.0
_DIAS = re.compile(r"^[0-9]{1,3}$")

# Lo que no mide la instancia, en las palabras que el agente le dice a la persona.
_GAP_TEXTO = {
    "conversaciones": "las conversaciones",
    "costo_modelo": "el costo del modelo",
    "costo_meta": "el costo de Meta",
    "monto_de_alguna_venta": "el monto de alguna venta",
}


@dataclass(frozen=True)
class SummaryConfig:
    enabled: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> SummaryConfig:
        return cls(enabled=env.get(ENV_ENABLED, "false").strip().lower() == "true")


# ---------------------------------------------------------------------------
# La ventana: dias enteros de la zona de la instancia.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    primero: date
    ultimo: date
    inicio: datetime
    fin: datetime
    parcial: bool

    @property
    def dias(self) -> int:
        return (self.ultimo - self.primero).days + 1


def _medianoche(dia: date, zona: ZoneInfo) -> datetime:
    return datetime(dia.year, dia.month, dia.day, tzinfo=zona).astimezone(UTC)


def _window(primero: date, ultimo: date, zona: ZoneInfo, now: datetime) -> Window:
    inicio = _medianoche(primero, zona)
    fin_dia = _medianoche(ultimo + timedelta(days=1), zona)
    fin = min(fin_dia, now)
    if inicio >= fin:
        raise ResultsRejected("window_empty")
    return Window(primero=primero, ultimo=ultimo, inicio=inicio, fin=fin, parcial=fin < fin_dia)


def parse_summary_window(
    desde: str | None, hasta: str | None, dias: str | None, *, tz: str, now: datetime
) -> tuple[Window, Window]:
    """La ventana pedida y la anterior, del mismo largo en dias y pegada antes.

    Con ``desde`` (y opcional ``hasta``): esos dias enteros. Con ``dias``: los ultimos N
    dias hasta ``hasta`` o hasta hoy, hoy incluido. Sin nada: los ultimos 7. ``desde`` y
    ``dias`` juntos no van."""
    zona = ZoneInfo(tz)
    hoy = now.astimezone(zona).date()
    # Un hasta futuro se recorta a hoy: los dias que no pasaron no se cuentan ni se comparan.
    ultimo = min(_dia(hasta, "hasta"), hoy) if hasta else hoy
    if desde and dias:
        raise ResultsRejected("window_conflict")
    if desde:
        primero = _dia(desde, "desde")
    else:
        n = DIAS_POR_DEFECTO
        if dias is not None:
            if not _DIAS.fullmatch(dias) or not 1 <= int(dias) <= MAX_DIAS:
                raise ResultsRejected("dias_invalid")
            n = int(dias)
        primero = ultimo - timedelta(days=n - 1)
    if primero > ultimo:
        raise ResultsRejected("window_empty")
    if (ultimo - primero).days + 1 > MAX_DIAS:
        raise ResultsRejected("window_too_long")
    actual = _window(primero, ultimo, zona, now)
    n = actual.dias
    anterior = _window(primero - timedelta(days=n), primero - timedelta(days=1), zona, now)
    if actual.parcial:
        # Con hoy parcial, la anterior se corta en el mismo punto: la misma cantidad de horas
        # desde su comienzo. Si no, «como venimos» saldria siempre por debajo.
        anterior = Window(primero=anterior.primero, ultimo=anterior.ultimo, inicio=anterior.inicio,
                          fin=anterior.inicio + (actual.fin - actual.inicio), parcial=True)
    return actual, anterior


# ---------------------------------------------------------------------------
# El resumen.
# ---------------------------------------------------------------------------

def _num(valor: object) -> float:
    return float(valor) if isinstance(valor, (int, float)) and not isinstance(valor, bool) else 0.0


def _ent(valor: object) -> int:
    return valor if isinstance(valor, int) and not isinstance(valor, bool) else 0


def usd(valor: float) -> str:
    """USD con la coma decimal y el punto de miles: 1.234,56."""
    entero, dec = f"{valor:,.2f}".split(".")
    return f"USD {entero.replace(',', '.')},{dec}"


def _plural(n: int, uno: str, varios: str) -> str:
    return f"{n} {uno if n == 1 else varios}"


def _dm(dia: date, ref: date) -> str:
    """dd/mm, con el año si no es el de ``ref`` (hoy de la instancia)."""
    return dia.strftime("%d/%m") if dia.year == ref.year else dia.strftime("%d/%m/%Y")


def _rango(w: Window, ref: date) -> str:
    if w.primero == w.ultimo:
        return f"El {_dm(w.primero, ref)}"
    return f"Del {_dm(w.primero, ref)} al {_dm(w.ultimo, ref)}"


def _numeros(body: Mapping[str, Any]) -> dict[str, object]:
    r = {row.get("metrica"): row.get("valor") for row in body.get("resumen") or [] if isinstance(row, Mapping)}
    envios = sum(_ent(f.get("envios")) for f in body.get("flujos") or [] if isinstance(f, Mapping))
    costo = r.get("costo_meta_usd")
    return {
        "ventas_recuperadas": _ent(r.get("ventas_recuperadas")),
        "ventas_directas": _ent(r.get("ventas_directas")),
        "ventas_atribuibles": _ent(r.get("ventas_atribuibles")),
        "neto_productor_usd": round(_num(r.get("neto_productor_usd")), 2),
        "neto_directas_usd": round(_num(r.get("neto_directas_usd")), 2),
        "neto_atribuibles_usd": round(_num(r.get("neto_atribuibles_usd")), 2),
        "compras_aprobadas": _ent(r.get("compras_aprobadas")),
        "envios_aceptados": envios,
        "costo_meta_usd": round(_num(costo), 2) if costo is not None else None,
    }


def _periodo(w: Window) -> dict[str, object]:
    return {"desde": w.primero.isoformat(), "hasta": w.ultimo.isoformat(), "dias": w.dias,
            "parcial": w.parcial, "since": w.inicio.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "until": w.fin.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _flujos(body: Mapping[str, Any]) -> tuple[list[dict[str, object]], int, int]:
    """Los flujos por ventas y envios (hasta 10), cuantos quedaron afuera y las ventas de todos."""
    filas = []
    for f in body.get("flujos") or []:
        if not isinstance(f, Mapping):
            continue
        ventas = _ent(f.get("ventas_directas")) + _ent(f.get("ventas_atribuibles"))
        filas.append({
            "flujo": f.get("flujo"), "ejecutor": f.get("ejecutor"), "envios_aceptados": _ent(f.get("envios")),
            "ventas": ventas,
            "neto_usd": round(_num(f.get("neto_directas_usd")) + _num(f.get("neto_atribuibles_usd")), 2),
        })
    filas.sort(key=lambda f: (-int(f["ventas"]), -int(f["envios_aceptados"]), str(f["flujo"]), str(f["ejecutor"])))
    return filas[:MAX_FLUJOS], max(0, len(filas) - MAX_FLUJOS), sum(int(f["ventas"]) for f in filas)


def _lineas(actual: Window, n: Mapping[str, Any], anterior: Window, na: Mapping[str, Any] | None,
            gaps: list[str], sin_flujo: int, ref: date) -> list[str]:
    hoy = " (hoy va parcial)" if actual.parcial else ""
    lineas = [
        f"{_rango(actual, ref)}{hoy}: "
        f"{_plural(int(n['ventas_recuperadas']), 'venta recuperada', 'ventas recuperadas')}, "
        f"{usd(float(n['neto_productor_usd']))} de comisión del productor.",
        f"Por un link nuestro: {n['ventas_directas']} ({usd(float(n['neto_directas_usd']))}). "
        f"Atribuibles por el caso o por la persona: {n['ventas_atribuibles']} "
        f"({usd(float(n['neto_atribuibles_usd']))}).",
        f"Mensajes de los flujos aceptados por WhatsApp: {n['envios_aceptados']}. "
        f"Compras aprobadas en la ventana, de todo origen: {n['compras_aprobadas']}.",
    ]
    if sin_flujo:
        lineas.append(f"{_plural(sin_flujo, 'venta no queda', 'ventas no quedan')} en ningún flujo de la lista.")
    if n["costo_meta_usd"] is not None:
        lineas.append(f"Costo de los mensajes de WhatsApp (Meta): {usd(float(n['costo_meta_usd']))}.")
    if na is not None:
        previo = "El día anterior" if anterior.dias == 1 else f"Los {anterior.dias} días anteriores"
        corte = ", hasta la misma hora" if anterior.parcial else ""
        lineas.append(
            f"{previo} ({_rango(anterior, ref)[0].lower()}{_rango(anterior, ref)[1:]}{corte}): "
            f"{_plural(int(na['ventas_recuperadas']), 'venta recuperada', 'ventas recuperadas')}, "
            f"{usd(float(na['neto_productor_usd']))}.")
    else:
        lineas.append("No pude leer los días anteriores para comparar.")
    no_mide = [_GAP_TEXTO[g] for g in gaps if g in _GAP_TEXTO]
    if no_mide:
        texto = no_mide[0] if len(no_mide) == 1 else ", ".join(no_mide[:-1]) + " y " + no_mide[-1]
        lineas.append(f"Esto no lo mido acá: {texto}.")
    return lineas


def build_summary(
    body: Mapping[str, Any], anterior_body: Mapping[str, Any] | None, *, actual: Window, anterior: Window,
    instancia: str, zona: str, hoy: date | None = None,
) -> dict[str, object]:
    """El resumen corto, desde la respuesta de /resultados de las dos ventanas."""
    numeros = _numeros(body)
    previos = None
    if anterior_body is not None:
        na = _numeros(anterior_body)
        previos = {k: na[k] for k in ("ventas_recuperadas", "ventas_directas", "ventas_atribuibles",
                                      "neto_productor_usd", "compras_aprobadas", "envios_aceptados")}
    flujos, omitidos, en_flujos = _flujos(body)
    # Una venta cuyo flujo no vino en la lista entra en los totales: se cuenta aparte.
    sin_flujo = max(0, int(numeros["ventas_recuperadas"]) - en_flujos)
    gaps = [str(g) for g in body.get("faltan") or []]
    if anterior_body is None:
        gaps.append("periodo_anterior_no_leido")
    return {
        "contract": CONTRACT,
        "contract_version": CONTRACT_VERSION,
        "fuente": body.get("version"),
        "regla": body.get("regla"),
        "instancia": instancia,
        "zona": zona,
        "periodo": _periodo(actual),
        "numeros": numeros,
        "por_flujo": flujos,
        "flujos_omitidos": omitidos,
        "ventas_sin_flujo": sin_flujo,
        "anterior": None if previos is None else {"periodo": _periodo(anterior), "numeros": previos},
        "lineas": _lineas(actual, numeros, anterior, previos, gaps, sin_flujo,
                          hoy if hoy is not None else actual.ultimo),
        "gaps": gaps,
    }


# ---------------------------------------------------------------------------
# La ruta.
# ---------------------------------------------------------------------------

def register_summary(
    app: FastAPI,
    *,
    supabase: Any,
    instance_ref: str,
    time_zone: str,
    require_token: Callable[[str | None], None],
    busy: Callable[[], None],
    slots: asyncio.Semaphore,
    failure: Callable[[Exception], HTTPException],
    results_config: ResultsConfig,
    pricing: Any | None,
    env: Mapping[str, str] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SummaryConfig:
    """Suma la ruta del resumen. Con su flag o el de /resultados apagado, 404."""
    config = SummaryConfig.from_env(os.environ if env is None else env)
    activa = config.enabled and results_config.enabled
    if not activa:
        # Apagado, la ruta no se registra: el bridge queda igual que sin este modulo.
        return config

    @app.get(ROUTE)
    async def automation_expert_results_summary(
        request: Request,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict[str, object]:
        require_token(authorization)
        q = request.query_params
        momento = now()
        try:
            actual, anterior = parse_summary_window(q.get("desde"), q.get("hasta"), q.get("dias"),
                                                    tz=time_zone, now=momento)
        except ResultsRejected as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.code) from None
        busy()
        prefijos = results_config.test_prefixes
        async with slots:
            try:
                raw = await read_results_v1(
                    supabase, rpc_payload(actual.inicio, actual.fin, tz=time_zone, test_prefixes=prefijos))
            except Exception as exc:  # noqa: BLE001 - el log lleva solo el tipo
                logger.warning("automation_expert_read_failed route=resultados_resumen error_type=%s",
                               type(exc).__name__)
                raise failure(exc) from None
            raw_anterior: Mapping[str, Any] | None
            try:
                raw_anterior = await read_results_v1(
                    supabase, rpc_payload(anterior.inicio, anterior.fin, tz=time_zone, test_prefixes=prefijos))
            except Exception as exc:  # noqa: BLE001 - sin la anterior, el resumen sale igual y lo dice
                logger.warning("automation_expert_results_summary_previous_failed error_type=%s",
                               type(exc).__name__)
                raw_anterior = None
        costo: list[dict[str, object]] | None = None
        if pricing is not None:
            try:
                # El plugin espera 15 s en total: Meta tiene aca un tope propio, corto.
                analitica = await asyncio.wait_for(pricing.pricing_analytics(actual.inicio, actual.fin),
                                                   META_TIMEOUT_SECONDS)
                costo = costo_por_dia(analitica, time_zone)
            except Exception as exc:  # noqa: BLE001 - sin costo, el resumen sale igual y lo dice
                logger.warning("automation_expert_results_meta_failed route=resultados_resumen error_type=%s",
                               type(exc).__name__)
        body = build_body(raw, instancia=instance_ref, zona=time_zone, desde=actual.inicio, hasta=actual.fin,
                          generado=momento, costo_meta=costo)
        body_anterior = None
        if raw_anterior is not None:
            body_anterior = build_body(raw_anterior, instancia=instance_ref, zona=time_zone,
                                       desde=anterior.inicio, hasta=anterior.fin, generado=momento,
                                       costo_meta=None)
        resumen = build_summary(body, body_anterior, actual=actual, anterior=anterior,
                                instancia=instance_ref, zona=time_zone,
                                hoy=momento.astimezone(ZoneInfo(time_zone)).date())
        logger.info("automation_expert_access route=resultados_resumen status=200")
        return resumen

    return config
