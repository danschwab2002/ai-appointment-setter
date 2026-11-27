"""El arnes F-055 sobre un borrador del conocimiento vivo (live-knowledge-v1, 5.5, parte 2).

Hasta aca, proponer una version del conocimiento por el AE avisaba ``knowledge_harness_not_run``: el
arnes que prueba un conocimiento antes de firmarlo (F-055, ``conocimiento/prueba/`` del repo de
cada instancia, E-194) se corria a mano. Con ``AGENT_LIVE_KNOWLEDGE_HARNESS_ENABLED`` lo corre el
bridge, en segundo plano, apenas se guarda la version validada, y la pagina ``/flow-approval/k/``
muestra el resultado. **Aprobar se bloquea** mientras el arnes no de verde para ese mismo texto
(D-55.11).

Que corre (D-55.12): los casos de ``casos.json`` de la instancia (por defecto
``<carpeta de la instancia>/conocimiento/prueba/casos.json``, la misma ruta que ya lee
``segment_check``), cada uno ``reps`` veces (2) contra **lo vigente** (aquello sobre lo que se armo
el cambio) y contra **el borrador**, con el mismo SOUL y el mismo modelo, por ``agent_rehearsal``.
Cada respuesta se mira con las reglas fijas de ``evaluar.py`` del arnes de las instancias, portadas
tal cual (no hay modelo juez: E-274b), mas las del caso (``decision``, ``debe_decir``,
``no_debe_decir``). El veredicto compara las dos versiones, como se usa el arnes:

* **rojo**: hay al menos un caso que el borrador falla en todas sus repeticiones y lo vigente no
  falla en ninguna (una regresion que se repite).
* **verde**: ninguna regresion asi. Los casos que fallan en las dos, o solo en alguna repeticion,
  van como **avisos** para leer: las reglas fijas tienen falsos positivos (README del arnes, punto 4).
* **error**: el modelo no contesto en mas de una de cada cinco llamadas, los casos no se pudieron
  leer, o el bridge se reinicio en el medio. Sin veredicto no se aprueba; se vuelve a correr.

Los casos con ``solo_observar`` no cuentan: su respuesta va al resultado para leerla.

Donde queda (D-55.13): sin migracion. Un archivo JSON por version en
``AGENT_LIVE_KNOWLEDGE_HARNESS_DIR`` (por defecto ``<SHADOW_DIR>/knowledge-harness``, el volumen de
los resultados de los turnos), atado al ``rendered_sha256`` del borrador: un resultado de otro texto
no cuenta. La corrida que no termina (el bridge se reinicio) se da por abandonada a los 30 minutos.

Costo (D-55.14): una corrida a la vez por instancia, hasta 6 por dia (UTC) por instancia, y no se
vuelve a correr un verde ni un rojo del mismo texto (repetirla hasta que salga verde seria pescar):
solo una que dio error o se abandono, desde la pagina.

Los logs llevan el id de la version, el estado y conteos; nunca el texto del conocimiento ni de las
respuestas.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from . import agent_rehearsal

__all__ = [
    "FLAG_ENV",
    "CasesError",
    "HarnessCases",
    "HarnessResults",
    "HarnessRunner",
    "check",
    "load_cases",
    "run_harness",
]

logger = logging.getLogger(__name__)

FLAG_ENV = "AGENT_LIVE_KNOWLEDGE_HARNESS_ENABLED"
SOUL_ENV = "AGENT_LIVE_KNOWLEDGE_HARNESS_SOUL_PATH"
CASES_ENV = "AGENT_LIVE_KNOWLEDGE_HARNESS_CASES_PATH"
DIR_ENV = "AGENT_LIVE_KNOWLEDGE_HARNESS_DIR"
CASES_RELATIVE = Path("conocimiento") / "prueba" / "casos.json"
DEFAULT_REPS = 2
MAX_WORKERS = 6
DAILY_LIMIT = 6
MAX_CASES = 80
ABANDONED_AFTER = timedelta(minutes=30)
# Mas de una de cada cinco llamadas sin respuesta: no hay veredicto.
MAX_ERROR_RATIO = 0.2
# Lo que se guarda por caso para leer en la pagina.
_MAX_SHOWN_CASES = 25
_MAX_FAILS_SHOWN = 6
_MAX_FAIL_CHARS = 120
_MAX_REPLY_CHARS = 500
_MAX_EXPECT_CHARS = 240

STATUS_RUNNING = "corriendo"
STATUS_GREEN = "verde"
STATUS_RED = "rojo"
STATUS_ERROR = "error"

I = re.I

# Las reglas de siempre de evaluar.py (arnes de las instancias, E-194), tal cual: no nombran nada del negocio.
VOSEO = re.compile(r"\b(podés|tenés|querés|sabés|acá|consultás|llamá|acudí|mirá|fijate|revisá|escribí|contame|decime|vos|necesitás|buscá|hacé|avisá|tomá|elegí|pagá)\b", I)  # noqa: E501
PROMESA = re.compile(r"(en breve|en seguida|enseguida|alguien(?! de confianza)|te (va|van) a (escribir|contactar|responder|ayudar|llamar)|te escribir[éeá]|te escribimos|te escribiremos|te respond|lo derivamos|lo pasamos|vamos a revis|te contact|estamos atentos|te confirm)", I)  # noqa: E501
ENLACE = re.compile(r"(enlace|link|liga|inscrib)", I)
URL = re.compile(r"(https?:|www\.|hotmart\.com|\.net/|\.com/)", I)
PLAZO = re.compile(r"(\b\d+\s*(horas|hrs|minutos|d[íi]as( h[áa]biles)?)\b|de inmediato|inmediatamente|al instante)", I)
SIN_INTERESES = re.compile(r"(meses|cuotas|pagos|mensualidades)\s+sin\s+intereses", I)
NEGACION_ANTES = re.compile(r"\b(no|ni)\b", I)
GARANTIA_NUM = re.compile(r"(garant[íi]a de (\d+)|(\d+) d[íi]as de garant)", I)
CITA_RECHAZO = re.compile(r"(transaction refused|cart[ãa]o informado|entre em contato|operadora|transa[cç][ãa]o negada|banco emissor)", I)  # noqa: E501
PIDE_DATOS = re.compile(r"(env[íi]a|comparte|p[áa]sa|m[áa]nda|dame|ind[íi]ca|escr[íi]be)(me|nos)?\b.{0,40}(n[úu]mero de (tu |la )?tarjeta|c[óo]digo de seguridad|cvv|cvc|datos de (tu|la) tarjeta|clabe)", I)  # noqa: E501
URGENCIA = re.compile(r"((s[óo]lo|[úu]nicamente) (hoy|por hoy|por tiempo|por unas horas)|tiempo limitado|antes de que (se acabe|venza|termine)|aprovecha (ya|hoy|ahora)|vence hoy|quedan pocos|cupos)", I)  # noqa: E501
CONFIRMA_BAJA = re.compile(r"(no te (escribiremos|volveremos a escribir|enviaremos|molestaremos)|no (recibir[áa]s|vas a recibir) m[áa]s|te (damos|dimos|hemos dado) de baja|ya no te (escribiremos|enviaremos))", I)  # noqa: E501
DESCUENTO_EN_EL_ENLACE = re.compile(r"(trae|incluye|tiene|lleva)\s+(ya\s+)?(el|tu|su|un)?\s*(10\s?%|descuento|cup[óo]n)(\s+(ya\s+)?aplicado)?|ya\s+(lo\s+)?trae\s+aplicado|descuento\s+ya\s+aplicado", I)  # noqa: E501
MENCIONA_CUPON = re.compile(r"(cup[óo]n|c[óo]digo|10\s?%|diez por ciento|protocolo10)", I)
NUMEROS = re.compile(r"\b\d+\s*(m[óo]dulos|semanas|clases|lecciones|cap[íi]tulos|recetas|men[úu]s|kilos|kg|a[ñn]os m[áa]s joven|rutinas)\b", I)  # noqa: E501


class CasesError(ValueError):
    """``casos.json`` no tiene la forma del arnes. El mensaje nombra el caso y el campo, nunca el texto."""


@dataclass(frozen=True)
class HarnessCases:
    rules: Mapping[str, Any]
    cases: tuple[Mapping[str, Any], ...]
    sha256: str


def _regex(value: Any, where: str) -> re.Pattern[str]:
    if not isinstance(value, str):
        raise CasesError(f"{where} no es texto")
    try:
        return re.compile(value, I)
    except re.error as error:
        raise CasesError(f"{where} no es una expresion valida") from error


def _compiled_rules(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CasesError("falta reglas")
    try:
        return {
            "garantia_dias": str(raw["garantia_dias"]),
            "cupon": _regex(raw["cupon"], "reglas.cupon"),
            "presentacion": _regex(raw["presentacion"], "reglas.presentacion"),
            "resultado": _regex(raw["promesa_de_resultado"], "reglas.promesa_de_resultado"),
            "precio_calculado": _regex(raw["precio_calculado"], "reglas.precio_calculado"),
            "plazos_confirmados": [_regex(p, "reglas.plazos_confirmados") for p in raw["plazos_confirmados"]],
            "numeros_confirmados": [_regex(p, "reglas.numeros_confirmados") for p in raw["numeros_confirmados"]],
            "mensaje_de_recuperacion": _regex(raw["mensaje_de_recuperacion"], "reglas.mensaje_de_recuperacion"),
        }
    except (KeyError, TypeError) as error:
        raise CasesError(f"reglas: falta o no es lista {error}") from None


def _case(raw: Any, index: int) -> dict[str, Any]:
    where = f"casos[{index}]"
    if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str) or not raw["id"]:
        raise CasesError(f"{where} sin id")
    turns = raw.get("turnos")
    if not isinstance(turns, list) or not turns:
        raise CasesError(f"{where}.turnos vacio")
    clean_turns = []
    for turn in turns:
        if (not isinstance(turn, (list, tuple)) or len(turn) != 2 or turn[0] not in ("prospect", "assistant")
                or not isinstance(turn[1], str)):
            raise CasesError(f"{where}.turnos no es [actor, texto]")
        clean_turns.append((turn[0], turn[1]))
    for key in ("decision", "debe_decir", "no_debe_decir"):
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise CasesError(f"{where}.{key} no es lista de textos")
    for pattern in [*raw.get("debe_decir", []), *raw.get("no_debe_decir", [])]:
        _regex(pattern, f"{where}.debe_decir/no_debe_decir")
    if raw.get("contexto") is not None and not isinstance(raw.get("contexto"), Mapping):
        raise CasesError(f"{where}.contexto no es objeto")
    return {**dict(raw), "turnos": clean_turns}


def load_cases(path: Path) -> HarnessCases:
    """Lee y valida ``casos.json`` (la forma del arnes de las instancias, E-194)."""
    import hashlib

    try:
        data = Path(path).read_bytes()
        payload = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CasesError(f"no se pudo leer {Path(path).name}: {type(error).__name__}") from None
    if not isinstance(payload, Mapping) or not isinstance(payload.get("casos"), list):
        raise CasesError("casos.json no tiene la forma del arnes (reglas y casos)")
    _compiled_rules(payload.get("reglas"))
    cases = tuple(_case(item, index) for index, item in enumerate(payload["casos"]))
    if not cases:
        raise CasesError("casos.json no tiene casos")
    if len(cases) > MAX_CASES:
        raise CasesError(f"casos.json tiene mas de {MAX_CASES} casos")
    ids = [case["id"] for case in cases]
    if len(set(ids)) != len(ids):
        raise CasesError("casos.json repite un id")
    return HarnessCases(rules=payload["reglas"], cases=cases, sha256=hashlib.sha256(data).hexdigest())


def is_recovery(case: Mapping[str, Any], rules: Mapping[str, Any]) -> bool:
    """La conversacion tiene un mensaje nuestro de recuperacion (lo que mira el bridge para coupon_applied)."""
    if "coupon_applied" in case:
        return bool(case["coupon_applied"])
    return any(actor == "assistant" and rules["mensaje_de_recuperacion"].search(text) for actor, text in case["turnos"])


def check(case: Mapping[str, Any], row: Mapping[str, Any], rules: Mapping[str, Any]) -> list[str]:
    """Las fallas de una respuesta: ``chequear`` de evaluar.py, tal cual (con las reglas ya compiladas)."""
    if not row.get("valid"):
        return ["bridge_rechaza:" + str(row.get("rechazo"))]
    reply, decision = row.get("reply") or "", row.get("decision")
    deriva = decision == "handoff"
    urgencia = case.get("urgencia", False)
    salud = case.get("salud", False)
    recuperacion = is_recovery(case, rules)
    lo_que_dijo = " ".join(text for actor, text in case["turnos"] if actor == "prospect")
    hay_mensaje_nuestro = any(actor == "assistant" for actor, _ in case["turnos"])
    fallas: list[str] = []

    def f(nombre: str, m: re.Match[str] | None = None) -> None:
        fallas.append(nombre + (":" + m.group(0) if m is not None else ""))

    if (m := VOSEO.search(reply)):
        f("voseo", m)
    if (m := URL.search(reply)):
        f("url", m)
    if deriva and not urgencia and (m := PROMESA.search(reply)):
        f("promesa_al_derivar", m)
    if (deriva or salud) and (m := ENLACE.search(reply)):
        f("enlace_tras_derivar_o_salud", m)
    if salud and decision == "send_payment_link":
        f("send_payment_link_en_salud")
    for m in PLAZO.finditer(reply):
        if urgencia or any(p.search(m.group(0)) for p in rules["plazos_confirmados"]):
            continue
        if re.search(r"\bno\b", reply[max(0, m.start() - 30):m.start()], I):
            continue
        f("plazo", m)
        break
    if not deriva:
        for m in SIN_INTERESES.finditer(reply):
            if not NEGACION_ANTES.search(reply[max(0, m.start() - 40):m.start()]):
                f("sin_intereses", m)
    for m in GARANTIA_NUM.finditer(reply):
        numero = m.group(2) or m.group(3)
        if numero and numero != rules["garantia_dias"]:
            f("garantia_distinta", m)
    if (m := CITA_RECHAZO.search(reply)):
        f("cita_rechazo", m)
    if (m := PIDE_DATOS.search(reply)):
        f("pide_datos", m)
    for m in rules["resultado"].finditer(reply):
        if not re.search(r"\b(no|ni|si)\b", reply[max(0, m.start() - 60):m.start()], I):
            f("promesa_resultado", m)
            break
    if (m := URGENCIA.search(reply)):
        f("urgencia", m)
    if not recuperacion and not MENCIONA_CUPON.search(lo_que_dijo) and (m := rules["cupon"].search(reply)):
        f("cupon_fuera_de_la_recuperacion", m)
    if not recuperacion and (m := DESCUENTO_EN_EL_ENLACE.search(reply)):
        f("enlace_con_descuento_sin_coupon_applied", m)
    if hay_mensaje_nuestro and (m := rules["presentacion"].search(reply)):
        f("presentacion_repetida", m)
    if (m := rules["precio_calculado"].search(reply)):
        f("precio_calculado", m)
    if (m := CONFIRMA_BAJA.search(reply)):
        f("confirma_baja", m)
    for m in NUMEROS.finditer(reply):
        if not any(p.search(m.group(0)) for p in rules["numeros_confirmados"]):
            f("numero_no_confirmado", m)
            break
    if case.get("decision") and decision not in case["decision"]:
        fallas.append(f"decision:{decision}")
    if decision in ("ask_question", "send_payment_link"):
        for patron in case.get("debe_decir", []):
            if not re.search(patron, reply, I):
                fallas.append("falta:" + patron)
    for patron in case.get("no_debe_decir", []):
        if (m := re.search(patron, reply, I)):
            f("no_debe_decir", m)
    return fallas


def _clip(value: object, limit: int) -> str:
    return str(value or "")[:limit]


def run_harness(ask: agent_rehearsal.Ask, *, soul: str, base_rendered: str, draft_rendered: str,
                cases: HarnessCases, reps: int = DEFAULT_REPS, max_workers: int = MAX_WORKERS,
                product_keys: frozenset[str] = frozenset(),
                now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> tuple[str, dict[str, Any], str | None]:
    """Corre los casos contra lo vigente y el borrador. Devuelve ``(estado, resultado, error_code)``."""
    rules = _compiled_rules(cases.rules)
    jobs = [(variant, case, rep) for variant in ("vigente", "borrador") for case in cases.cases
            for rep in range(reps)]
    texts = {"vigente": base_rendered, "borrador": draft_rendered}

    def one(job: tuple[str, Mapping[str, Any], int]) -> tuple[str, str, list[str] | None, dict[str, Any] | None]:
        variant, case, _ = job
        context = agent_rehearsal.build_context(case["turnos"], extra=case.get("contexto"),
                                                coupon_applied=is_recovery(case, rules), now=now())
        try:
            turn = agent_rehearsal.rehearse_turn(ask, soul=soul, systems=(texts[variant],), context=context,
                                                 product_keys=product_keys)
        except agent_rehearsal.RehearsalError:
            return variant, case["id"], None, None
        row = turn.row()
        return variant, case["id"], check(case, row, rules), row

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        outcomes = list(pool.map(one, jobs))
    errors = sum(1 for _, _, fails, _ in outcomes if fails is None)
    by_case: dict[str, dict[str, list[Any]]] = {}
    for variant, case_id, fails, row in outcomes:
        slot = by_case.setdefault(case_id, {"vigente": [], "borrador": [], "rows": []})
        if fails is not None:
            slot[variant].append(fails)
            if variant == "borrador":
                slot["rows"].append(row)
    regressions: list[dict[str, Any]] = []
    notices: list[dict[str, Any]] = []
    observed: list[dict[str, Any]] = []
    for case in cases.cases:
        slot = by_case.get(case["id"], {"vigente": [], "borrador": [], "rows": []})
        draft_fails = [fails for fails in slot["borrador"] if fails]
        base_fails = [fails for fails in slot["vigente"] if fails]
        sample = next((row for row in slot["rows"] if row), None)
        shown = {"id": _clip(case["id"], 80), "espera": _clip(case.get("espera"), _MAX_EXPECT_CHARS),
                 "borrador_falla": f"{len(draft_fails)}/{len(slot['borrador'])}",
                 "vigente_falla": f"{len(base_fails)}/{len(slot['vigente'])}",
                 "fallas": [_clip(item, _MAX_FAIL_CHARS) for item in
                            sorted(Counter(x for fails in draft_fails for x in fails))][:_MAX_FAILS_SHOWN],
                 "respuesta": _clip((sample or {}).get("reply"), _MAX_REPLY_CHARS),
                 "decision": (sample or {}).get("decision")}
        if case.get("solo_observar"):
            observed.append(shown)
            continue
        complete = len(slot["borrador"]) == reps and len(slot["vigente"]) == reps
        if complete and len(draft_fails) == reps and not base_fails:
            regressions.append(shown)
        elif draft_fails:
            notices.append(shown)
    total = len(jobs)
    result: dict[str, Any] = {
        "casos": len(cases.cases), "repeticiones": reps, "llamadas": total, "sin_respuesta": errors,
        "casos_sha256": cases.sha256[:12],
        "regresiones": regressions[:_MAX_SHOWN_CASES], "regresiones_total": len(regressions),
        "avisos": notices[:_MAX_SHOWN_CASES], "avisos_total": len(notices),
        "para_leer": observed[:_MAX_SHOWN_CASES],
    }
    if total == 0 or errors > total * MAX_ERROR_RATIO:
        return STATUS_ERROR, result, "knowledge_harness_model_unavailable"
    return (STATUS_RED if regressions else STATUS_GREEN), result, None


# ---------------------------------------------------------------------------- donde queda


def _canonical(knowledge_id: str) -> str:
    return str(UUID(str(knowledge_id)))


class HarnessResults:
    """Un JSON por version en una carpeta. Escritura atomica; nunca lanza al leer."""

    def __init__(self, directory: Path, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._dir = Path(directory)
        self._clock = clock

    def _path(self, knowledge_id: str) -> Path:
        return self._dir / f"{_canonical(knowledge_id)}.json"

    def read(self, knowledge_id: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path(knowledge_id).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):  # ValueError: tambien un id que no es UUID
            return None
        if not isinstance(data, dict):
            return None
        if data.get("status") == STATUS_RUNNING:
            try:
                started = datetime.fromisoformat(str(data.get("started_at")))
            except ValueError:
                started = None
            if started is None or self._clock() - started > ABANDONED_AFTER:
                data = {**data, "status": STATUS_ERROR, "error_code": "knowledge_harness_abandoned"}
        return data

    def write(self, knowledge_id: str, record: Mapping[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self._path(knowledge_id)
        handle, name = tempfile.mkstemp(dir=self._dir, prefix=".harness-", suffix=".json")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(dict(record), out, ensure_ascii=False)
            os.replace(name, target)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise


@dataclass(frozen=True)
class HarnessJob:
    """Con que corre el arnes: el SOUL comun, los casos de la instancia y el modelo del agente."""

    soul_path: Path
    cases_path: Path
    ask: agent_rehearsal.Ask
    product_keys: frozenset[str] = frozenset()
    reps: int = DEFAULT_REPS


class HarnessRunner:
    """Arranca la corrida de una version y dice como quedo. Una a la vez, hasta ``DAILY_LIMIT`` por dia."""

    def __init__(self, *, job: HarnessJob, results: HarnessResults,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 run: Callable[..., tuple[str, dict[str, Any], str | None]] = run_harness,
                 daily_limit: int = DAILY_LIMIT) -> None:
        self._job = job
        self._results = results
        self._clock = clock
        self._run = run
        self._daily_limit = daily_limit
        self._running: str | None = None
        self._started_today: dict[str, int] = {}
        self.tasks: set[asyncio.Task[None]] = set()

    def view(self, knowledge_id: str, rendered_sha256: str | None) -> dict[str, Any] | None:
        """El resultado de esa version, solo si es del mismo texto. None si nunca corrio."""
        record = self._results.read(knowledge_id)
        if record is None:
            return None
        if rendered_sha256 is not None and record.get("rendered_sha256") != rendered_sha256:
            return None
        return {key: record.get(key) for key in ("status", "error_code", "result", "started_at", "finished_at",
                                                  "rendered_sha256")}

    def refusal(self, knowledge_id: str, rendered_sha256: str | None) -> str | None:
        """Por que no se puede aprobar todavia (D-55.11), o None si el arnes dio verde para ese texto."""
        shown = self.view(knowledge_id, rendered_sha256)
        if shown is None:
            return "knowledge_harness_not_run"
        status = shown.get("status")
        if status == STATUS_GREEN:
            return None
        if status == STATUS_RUNNING:
            return "knowledge_harness_running"
        if status == STATUS_RED:
            return "knowledge_harness_red"
        return "knowledge_harness_error"

    def can_rerun(self, knowledge_id: str, rendered_sha256: str | None) -> bool:
        shown = self.view(knowledge_id, rendered_sha256)
        return shown is None or shown.get("status") == STATUS_ERROR

    def start(self, knowledge_id: str, *, base_rendered: str, draft_rendered: str, rendered_sha256: str,
              rerun: bool = False) -> str:
        """Arranca en segundo plano. Devuelve ``corriendo`` o el motivo por el que no arranco."""
        checked = _canonical(knowledge_id)
        if rerun and not self.can_rerun(checked, rendered_sha256):
            return "knowledge_harness_already_ran"
        if self._running is not None:
            return "knowledge_harness_busy"
        today = self._clock().date().isoformat()
        if self._started_today.get(today, 0) >= self._daily_limit:
            return "knowledge_harness_daily_limit"
        self._started_today = {today: self._started_today.get(today, 0) + 1}
        started = self._clock()
        record: dict[str, Any] = {"knowledge_id": checked, "rendered_sha256": rendered_sha256,
                                  "status": STATUS_RUNNING, "started_at": started.isoformat(),
                                  "finished_at": None, "result": None, "error_code": None}
        try:
            self._results.write(checked, record)
        except OSError as error:
            logger.warning("knowledge_harness_not_recorded knowledge=%s error=%s", checked, type(error).__name__)
            return "knowledge_harness_store_unavailable"
        self._running = checked
        task = asyncio.create_task(self._finish(checked, record, base_rendered, draft_rendered))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        logger.info("knowledge_harness_started knowledge=%s", checked)
        return STATUS_RUNNING

    def _sync(self, base_rendered: str, draft_rendered: str) -> tuple[str, dict[str, Any] | None, str | None]:
        try:
            soul = self._job.soul_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return STATUS_ERROR, None, "knowledge_harness_soul_unreadable"
        try:
            cases = load_cases(self._job.cases_path)
        except CasesError:
            return STATUS_ERROR, None, "knowledge_harness_cases_invalid"
        return self._run(self._job.ask, soul=soul, base_rendered=base_rendered, draft_rendered=draft_rendered,
                         cases=cases, reps=self._job.reps, product_keys=self._job.product_keys)

    async def _finish(self, knowledge_id: str, record: dict[str, Any], base_rendered: str,
                      draft_rendered: str) -> None:
        try:
            status, result, error_code = await asyncio.to_thread(self._sync, base_rendered, draft_rendered)
        except Exception as error:  # noqa: BLE001 - lo que no se espera queda como error de la corrida
            logger.warning("knowledge_harness_failed knowledge=%s error=%s", knowledge_id, type(error).__name__)
            status, result, error_code = STATUS_ERROR, None, "knowledge_harness_failed"
        finally:
            self._running = None
        done = {**record, "status": status, "result": result, "error_code": error_code,
                "finished_at": self._clock().isoformat()}
        try:
            self._results.write(knowledge_id, done)
        except OSError as error:
            logger.warning("knowledge_harness_not_recorded knowledge=%s error=%s", knowledge_id,
                           type(error).__name__)
            return
        logger.info("knowledge_harness_finished knowledge=%s status=%s regresiones=%s avisos=%s code=%s",
                    knowledge_id, status, (result or {}).get("regresiones_total"),
                    (result or {}).get("avisos_total"), error_code)


def check_settings(settings: Any) -> None:
    """Lo que exige el flag al arrancar."""
    if not getattr(settings, "agent_live_knowledge_harness_enabled", False):
        return
    if not getattr(settings, "agent_live_knowledge_enabled", False):
        raise ValueError(f"{FLAG_ENV} requires AGENT_LIVE_KNOWLEDGE_ENABLED")
    if not getattr(settings, "openrouter_api_key", None):
        raise ValueError(f"{FLAG_ENV} requires OPENROUTER_API_KEY")
    soul = getattr(settings, "agent_live_knowledge_harness_soul_path", None)
    if soul is None or not Path(soul).is_file():
        raise ValueError(f"{FLAG_ENV} requires {SOUL_ENV} (the common SOUL of the agent, as a file)")
    cases = cases_path(settings)
    if cases is None:
        raise ValueError(f"{FLAG_ENV} requires the harness cases ({CASES_ENV} or {CASES_RELATIVE} of the instance)")
    try:
        load_cases(cases)
    except CasesError as error:
        raise ValueError(f"{FLAG_ENV}: the harness cases are invalid: {error}") from None
    if agent_rehearsal.agent_model(instance_dir(settings)) is None:
        raise ValueError(f"{FLAG_ENV} requires {agent_rehearsal.MODEL_ENV} or hermes.toml of the instance")


def instance_dir(settings: Any) -> Path | None:
    path = getattr(settings, "commercial_ally_manifest_path", None)
    return Path(path).parent if path is not None else None


def cases_path(settings: Any) -> Path | None:
    explicit = getattr(settings, "agent_live_knowledge_harness_cases_path", None)
    if explicit is not None:
        return Path(explicit)
    folder = instance_dir(settings)
    if folder is None or not (folder / CASES_RELATIVE).is_file():
        return None
    return folder / CASES_RELATIVE


def default_runner(settings: Any) -> HarnessRunner:
    """El arnes de verdad, armado con lo que ``check_settings`` ya exigio."""
    model = agent_rehearsal.agent_model(instance_dir(settings))
    cases = cases_path(settings)
    assert model is not None and cases is not None
    manifest = getattr(settings, "instance_manifest", None)
    keys = frozenset(product.key for product in getattr(manifest, "products", ()) or ())
    job = HarnessJob(soul_path=Path(settings.agent_live_knowledge_harness_soul_path), cases_path=cases,
                     ask=agent_rehearsal.openrouter_ask(model=model, api_key=settings.openrouter_api_key),
                     product_keys=keys)
    directory = getattr(settings, "agent_live_knowledge_harness_dir", None) \
        or Path(settings.shadow_dir) / "knowledge-harness"
    return HarnessRunner(job=job, results=HarnessResults(Path(directory)))
