# Evidencia local — perímetro Lancemos fase 1

- **Fecha:** 2026-08-10
- **Estado:** evidencia local; no es despliegue ni activación
- **Rama:** `feat/lancemos-pilot-boundary`
- **Scope:** migración, contrato y probes de perímetro default-off

## Resultado

La fase 1 implementa de forma aditiva:

- scope publicado e inmutable para tenant/account/inbox/cuenta de canal/producto/oferta;
- control runtime default `inactive` con generación CAS;
- cohorte explícita con cap;
- presupuesto conservador total y diario contado por request-start autorizado;
- ledger append-only e idempotencia por attempt;
- kill switch serializado contra nuevas autorizaciones;
- ACL cerrada para roles API y DML directo de `service_role`.

No existe seed con identificadores reales y no se modificó el runtime Python. La allowlist actual permanece. El ingreso autoritativo de abandono ya está integrado en `main`; componer el perímetro con admisión, planificación y `mark_followup_request_started` pertenece a una fase posterior.

## PGlite

Comando:

```text
node tests/sql/followup_engine/validate_pilot_boundary.mjs
```

Marcadores obtenidos:

```text
pilot_default_off=OK
pilot_cohort_cap=OK
pilot_scope_conjunction=OK
pilot_budget_and_replay=OK
pilot_kill_switch=OK
pilot_generation_fence=OK
pilot_scope_version_activation=OK
pilot_effective_privileges=OK
pilot_closed_is_irreversible=OK
pilot_immutability_and_audit=OK
LANCEMOS_PILOT_BOUNDARY_OK
```

El probe emuló antes de aplicar migraciones:

- grants default de `EXECUTE` a `anon/authenticated`;
- grants default de tabla a `service_role`.

Luego verificó que los ocho objetos funcionales nuevos no filtraran `EXECUTE` a roles API, que sólo cinco entrypoints fueran ejecutables por `service_role`, y que las cinco tablas negaran DML directo.

## PostgreSQL real

Se descargó y extrajo PostgreSQL 17.10 temporalmente bajo `/tmp`, sin instalación global. Se creó una base descartable cuyo nombre comenzaba con `pilot_boundary_concurrency`, protegida además por:

```text
ALLOW_DISPOSABLE_DATABASE=pilot-boundary-concurrency
```

Comando del probe:

```text
uv run python tests/sql/followup_engine/real_postgres_pilot_boundary.py
```

Marcadores obtenidos:

```text
pilot_boundary_real_postgres_migrations=OK
pilot_boundary_effective_privileges=OK
pilot_boundary_real_postgres_version_activation=OK
pilot_boundary_real_postgres_cohort_concurrency=OK
pilot_boundary_real_postgres_exact_replay_concurrency=OK
pilot_boundary_real_postgres_budget_concurrency=OK
pilot_boundary_real_postgres_kill_switch=OK
```

El probe demostró:

1. baseline y todas las migraciones aplican en PostgreSQL 17.10;
2. defaults estilo Supabase no dejan privilegios efectivos inesperados;
3. dos inscripciones concurrentes con cap uno producen un único miembro activo;
4. la generación CAS rechaza el writer concurrente obsoleto y el retry observa el cap;
5. dos autorizaciones concurrentes del mismo attempt producen un insert y un replay durable;
6. dos attempts distintos concurrentes respetan el cap diario sin excederlo;
7. después de pausar, un intento nuevo retorna `pilot_runtime_not_armed`.

Después de una primera revisión independiente `REQUEST_CHANGES`, se agregaron regresiones y correcciones para:

- policy key/version canónica del caso;
- fecha presupuestaria derivada del reloj de PostgreSQL y `p_now` acotado;
- replay durable exacto después de pausa, sin permitir parámetros de scope distintos;
- activación V2/rollback sólo desde pausa o inactividad y siempre default-off;
- presupuesto acumulado por `scope_key`, sin reinicio por cambio de versión;
- timezone constante por `scope_key`, sin rollover de fecha diaria entre versiones;
- relectura del ledger bajo lock para replay concurrente del mismo attempt;
- ACL del nuevo RPC de activación.

Tras integrar el Workstream B se adaptaron los fixtures del perímetro para ingresar abandonos mediante `admit_hotmart_cart_abandonment`, en lugar de fabricar eventos no autoritativos. Se repitieron la suite combinada, PGlite y PostgreSQL 17.10 real con las 14 migraciones y resultado PASS. CI ejecuta el probe directamente después de `npm test`, y `docs/architecture.md` ya registra autoridades, fronteras y límites de esta fase.

La revisión técnica independiente final emitió `APPROVE`, sin bloqueantes técnicos nuevos. Confirmó en PostgreSQL 17.10 tanto el replay concurrente exacto como la serialización de versiones concurrentes con timezone incompatible.

PostgreSQL se detuvo y se eliminaron binarios, data directory, socket y artefactos temporales.

## Límites de la evidencia

Esto no prueba:

- migración aplicada en Supabase;
- scope real publicado;
- runtime armado;
- configuración WABA;
- wiring con el bridge o request-start;
- prueba HTTP;
- mensajes reales;
- E2E productivo.

Hasta completar fase 2, la capacidad correcta es: **mecanismo SQL local implementado y probado; perímetro ejecutable end-to-end todavía incompleto**.
