# Evidencia local — handoff humano ejecutable

- **Fecha:** 2026-08-10
- **Árbol:** `feat/executable-human-handoff-runtime`
- **Alcance:** migración y runtime local, sin servicios ni datos productivos
- **No acredita:** deploy, migración remota, Chatwoot real, equipo real, mensajes enviados ni piloto activo

## Suite Python

```text
uv run pytest -q
resultado: PASS, 570 tests recolectados
```

Incluye contrato Hermes, admisión determinística, adaptador Supabase, matriz Chatwoot, worker reconciliador, configuración default-off y HTTP real local.

## HTTP real controlado

```text
uv run pytest -q tests/test_handoff_http_e2e.py -vv
resultado: 1 passed
```

La prueba abre Uvicorn sobre TCP local, consulta `/ready` con proyección habilitada y una autoridad stateful controlada, confirma conteos sanitizados y confirma cero requests Chatwoot. No usa TestClient ni servicios externos.

## SQL PGlite

```text
cd tests/sql/followup_engine
npm test
```

Marcadores específicos:

```text
HANDOFF_DURABLE_STOP_OK
HANDOFF_REPLAY_AND_EVIDENCE_OK
HANDOFF_PROJECTION_LEASES_OK
HANDOFF_EFFECTIVE_ACL_OK
```

La validación cubre stop durable, replay/evidencia, rechazo de cambios de policy sobre un request existente, snapshots inmutables, claims/finalizaciones con reloj de PostgreSQL y fencing, drain del efecto hermano tras un dead letter, desactivación con drain y privilegios efectivos de `anon`, `authenticated` y `service_role`.

## PostgreSQL 17 real y carreras de request-start

Se inició un PostgreSQL 17 descartable local en socket Unix y se aplicaron baseline y todas las migraciones sobre bases vacías. Luego se ejecutó:

```text
uv run python tests/sql/followup_engine/validate_handoff_postgres.py \
  --pg-bin /tmp/pg17-root/usr/lib/postgresql/17/bin \
  --host /tmp/handoff-pgsock \
  --port 55432
```

Resultado:

```text
HANDOFF_EFFECTIVE_ACL_EXHAUSTIVE_OK
HANDOFF_CONCURRENT_EXACT_REPLAY_OK
HANDOFF_CONCURRENT_CONFLICT_REJECTED_OK
HANDOFF_COMMIT_BLOCKED_REQUEST_START_OK
STARTED_REQUEST_PRESERVED_UNKNOWN_OK
LATE_ACCEPTANCE_NO_SUCCESSOR_OK
```

El validador comprueba primero todos los privilegios efectivos de tablas y funciones del handoff, y ejecuta dos sesiones con la misma command key: el replay exacto espera y responde `already_requested`, mientras el replay semánticamente conflictivo falla cerrado.

La carrera de request-start mantiene abierta la transacción de handoff después del stop mientras una segunda sesión intenta `mark_lancemos_pilot_request_started`. El inicio espera al commit y falla; el intento queda `failed_before_request`, sin autorización de request-start, y caso/secuencia/conversación permanecen pausados.

La carrera inversa mantiene abierta la transacción que confirma `request_started`
mientras otra sesión intenta el handoff. El handoff espera el commit y conserva
el intento como `delivery_unknown`. Una aceptación Chatwoot 30 días más tarde se registra
como `accepted_by_chatwoot` sin reabrir el caso ni crear un sucesor.

El validador crea y elimina sus propias bases descartables. CI ejecuta la misma prueba contra un servicio PostgreSQL 17 efímero.

## Seguridad y límites

- Admisión y proyección siguen default-off.
- No se llamó a una API Chatwoot real.
- No se usaron credenciales productivas.
- No se aplicó ninguna migración remota.
- No se ejecutó deploy ni se activó cohorte.
- `git diff --check` terminó sin errores antes de la revisión final.
