# Evidencia local — wiring runtime del perímetro Lancemos

- **Fecha:** 2026-08-10
- **Estado:** Evidencia local; implementación todavía no desplegada
- **Rama:** `feat/lancemos-pilot-boundary-runtime-wiring`
- **Alcance:** planificación y request-start atómicos, binding durable del scope, readiness y configuración default-off

## Entorno

- Python 3.13 administrado con `uv`.
- PGlite provisto por el harness SQL del repositorio.
- PostgreSQL 17.10 disposable levantado localmente desde paquetes extraídos, sin Docker ni acceso a EasyPanel.
- Bases desechables separadas para los probes de fase 1 y runtime.

No se usaron IDs ni credenciales productivas. No se desplegaron migraciones, no se activó una cohorte, no se ejecutaron workers productivos y no se enviaron mensajes.

## Evidencia verificada

### Python

```text
uv run pytest -q
RESULTADO: PASS
```

La suite completa pasó. Sólo se observó el warning preexistente de deprecación de `starlette.testclient` respecto de `httpx`.

Los tests cubrieron además el factory productivo sin sender inyectado para un
scope WABA, la exigencia fail-fast de templates aprobados y los payloads
`template_params` separados para primer contacto y seguimiento.

### SQL/PGlite

```text
npm test
node validate_pilot_boundary.mjs
node validate_pilot_boundary_runtime.mjs
RESULTADO: PASS
```

El probe runtime comprobó:

- default-off;
- planificación atómica;
- binding inmutable `caso → scope/version → evento admitido`;
- bloqueo del request-start histórico;
- request-start atómico y replay;
- rechazo de `waba + freeform` antes de request-start;
- ACL efectivas, incluida ausencia de DML de `service_role` sobre el binding.

### PostgreSQL 17 real

```text
uv run python tests/sql/followup_engine/real_postgres_pilot_boundary.py
RESULTADO: PASS

uv run python tests/sql/followup_engine/real_postgres_pilot_boundary_runtime.py
RESULTADO: PASS
```

Los probes aplicaron todas las migraciones desde cero y verificaron:

- privilegios efectivos;
- activación versionada;
- concurrencia de cohorte y presupuesto;
- replay idempotente;
- kill switch;
- planificación default-off;
- carrera pausa/request-start;
- rechazo durable de `waba + freeform`;
- request-start y replay atómicos.

### Checks estáticos

```text
uv run python -m compileall -q src tests
RESULTADO: PASS

git diff --check
RESULTADO: PASS
```

`ruff` no forma parte de las dependencias instaladas del proyecto y no estuvo disponible localmente; no se ocultó ni sustituyó ese gate.

## Límites de esta evidencia

Esta evidencia prueba el árbol local y PostgreSQL disposable. No prueba:

- despliegue en EasyPanel;
- migraciones en Supabase productivo;
- disponibilidad de WABA;
- recepción real de Hotmart;
- envío real de WhatsApp;
- activación de una cohorte Lancemos.

Esas afirmaciones requieren una ejecución de despliegue y un go/no-go separado.
