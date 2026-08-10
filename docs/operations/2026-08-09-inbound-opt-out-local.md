# Verificación local del opt-out inbound durable — 2026-08-09/10

- **Estado:** evidencia local; no es evidencia de despliegue
- **Scope:** migraciones desde cero, bridge, worker de proyección y carreras de `request_started`
- **Datos reales:** ninguno
- **Mensajes externos:** ninguno

## Resultado

La implementación local conserva Supabase como autoridad de la baja, bloquea
Hermes antes del razonamiento y proyecta macro/labels hacia Chatwoot mediante una
cola con lease. No se aplicó la migración en Supabase productivo ni se inició un
worker productivo.

## Pruebas ejecutadas

```text
uv run pytest -q
  exit 0

npm test --prefix tests/sql/followup_engine
  exit 0
  INBOUND_OPT_OUT_DURABLE_OK

uv run python -m compileall -q src tests
  exit 0

git diff --check
  exit 0
```

Ruff se ejecutó mediante `uvx ruff`. La comparación estructurada de los mismos
paths contra el checkout de integración devolvió 23 hallazgos preexistentes en
ambos árboles y cero hallazgos nuevos (`new_issue_count=0`).

## PostgreSQL real

Se levantó un PostgreSQL 17.10 local y descartable sin credenciales externas. El
probe `tests/sql/followup_engine/real_postgres_opt_out.py` aplicó baseline y todas
las migraciones desde cero y devolvió:

```text
optout_real_postgres_migrations=OK
optout_effective_privileges=OK
optout_reverse_order_request_start=OK
optout_real_postgres_concurrency=OK
```

El probe comprobó:

- antes de aplicar baseline/migraciones emuló los default privileges directos de
  Supabase concediendo `EXECUTE` a `anon` y `authenticated`;
- después inventarió dinámicamente todo `pg_proc` público con `prosecdef` y exigió
  cero funciones ejecutables por esos dos roles;
- `service_role` puede leer intentos pero no insertar, actualizar ni borrar filas;
- los entrypoints necesarios siguen siendo ejecutables sólo por `service_role`;
  `anon` y `authenticated` no pueden ejecutar request-start, reconciliación ni el
  helper interno de cierre `not_applied`;
- un stop `unmatched` admitido antes de crear la identidad bloquea un
  `request_started` posterior;
- dos sesiones reales se superponen sobre el advisory lock por cuenta + usuario;
- la sesión de request-start espera el lock, observa el stop confirmado y falla
  cerrado.

El cluster, la base y los paquetes temporales se eliminaron al terminar.

## Límites de esta evidencia

No demuestra:

- migración aplicada en Supabase remoto;
- PostgREST real usando los grants nuevos;
- macro configurado en el inbox productivo;
- WABA, Hotmart o WhatsApp productivos;
- envío, recepción o baja E2E de un contacto real.
