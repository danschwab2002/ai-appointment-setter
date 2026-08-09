# Verificación local de ingreso PURCHASE_APPROVED — 2026-08-08

## Alcance

Validar la implementación local del ingreso durable de una compra aprobada de
Hotmart antes de aplicar la migración en Supabase o usar activos de Lancemos.

No se usaron credenciales reales, números reales ni datos personales reales.

## Estado del código

- rama aislada: `feat/lancemos-purchase-cancellation`;
- base: `9538600`;
- migraciones: `20260808000100_hotmart_purchase_approved.sql` y
  `20260808000200_hotmart_purchase_ordering_guard.sql`;
- contrato: `docs/contracts/hotmart-purchase-approved-v1.md`.

## Verificaciones ejecutadas

### Suite

```text
uv run pytest -q
resultado: exit 0
colección: 365 tests
```

### Sintaxis PostgreSQL

Las migraciones fueron procesadas con `pglast`:

```text
20260808000100_hotmart_purchase_approved.sql: 6 statements
20260808000200_hotmart_purchase_ordering_guard.sql: 7 statements
```

### Ejecución SQL conductual local

Se aplicaron el baseline y todas las migraciones en PGlite y se ejecutó el RPC
real sobre filas de prueba sanitizadas:

```text
DIRECT_PURCHASE_APPLIED_OK
PURCHASE_BEFORE_ABANDONMENT_GUARD_OK
IN_FLIGHT_DELIVERY_PRESERVED_AS_UNKNOWN_OK
```

Esto prueba semántica PostgreSQL local, incluida la transición atómica y la
guarda de orden inverso. PGlite no sustituye a la instancia Supabase real ni a
una prueba de concurrencia multiproceso.

### HTTP real local

Se inició:

1. un servidor HTTP local que simuló únicamente el endpoint de inserción de
   PostgREST;
2. Uvicorn con `bridge.app:build_app` y workers salientes deshabilitados.

Resultados observados:

```text
GET /health
HTTP 200
{"status":"ok"}

POST /webhooks/hotmart
HTTP 202
{"status":"received","event_id":"purchase-http-local-001"}
```

La solicitud que el bridge envió al simulador de PostgREST fue validada de
forma programática:

```text
source = hotmart
external_event_id = purchase-http-local-001
event_type = PURCHASE_APPROVED
processing_status = received
data.purchase.status = APPROVED
```

Los dos servidores locales fueron detenidos después de la prueba.

## Qué demuestra

- el endpoint admite `PURCHASE_APPROVED` v2.0.0;
- aplica autenticación y anti-replay existentes;
- responde `202` sólo después de una inserción HTTP aceptada;
- persiste el tipo correcto para procesamiento diferido;
- la nueva ruta no rompe la suite local;
- el RPC cierra caso y secuencia y cancela una acción no iniciada;
- una compra conocida antes del abandono cancela el plan recién creado;
- una entrega con request iniciado queda `delivery_unknown`, no `cancelled`.

## Qué no demuestra

- que las migraciones ejecuten contra el Postgres real de Supabase;
- que la función correlacione correctamente datos reales;
- que un caso, secuencia y acción de la instancia Supabase real cambien de estado;
- que Hotmart entregue este payload exacto para la cuenta de Lancemos;
- que el cierre funcione end-to-end en producción.

Antes de declarar la capacidad operativa deben aplicarse la migración de forma
controlada y verificar los outcomes `applied`, `already_applied`, `not_found` y
`ambiguous` contra datos de prueba en el esquema real.

## Addendum de integración — 2026-08-09

La rama fue actualizada contra el `main` vigente y la admisión HTTP de compra
pasó de un insert REST genérico a la RPC transaccional
`admit_hotmart_purchase_approved(...)`. Por eso, el simulador HTTP descrito
arriba conserva valor histórico pero ya no representa el endpoint PostgREST
actual.

La verificación combinada ejecutó:

```text
uv run pytest -q: exit 0
npm test --prefix tests/sql/followup_engine: exit 0
validate-tree: 11 migraciones, 0 versiones duplicadas
compileall: exit 0
Ruff focalizado sobre pruebas modificadas: exit 0
git diff --check: exit 0
```

Los probes SQL nuevos produjeron:

```text
PURCHASE_SEMANTIC_EXACT_REPLAY_OK
PURCHASE_SEMANTIC_CONFLICT_FAILS_CLOSED_OK
UNPROCESSABLE_PURCHASE_DOES_NOT_RESERVE_TRANSACTION_OK
LEGACY_MALFORMED_PURCHASE_CANNOT_SUPPRESS_CORRECTION_OK
UNRESOLVED_PURCHASE_SEMANTIC_CONFLICT_BLOCKS_REQUEST_START_OK
RESOLVED_PURCHASE_SEMANTIC_CONFLICT_REPLAY_OK
```

También se levantaron Uvicorn y un stub PostgREST en puertos TCP locales. Dos
requests HTTP reales verificaron el mapeo bridge → RPC:

```text
PURCHASE_HTTP_RPC_ADMISSION_OK
PURCHASE_HTTP_SEMANTIC_CONFLICT_MAPPING_OK
```

Esto demuestra localmente que una repetición con tupla normalizada idéntica no
crea trabajo, mientras que una misma transacción con datos de negocio distintos
persiste un incidente y bloquea transaccionalmente todo `request_started`
posterior. También demuestra que tipos no procesables no reservan la transacción
y que el replay posterior a una resolución conserva su outcome sin reabrirla.
Todavía no demuestra que la migración forward esté aplicada en Supabase ni que
el bridge desplegado invoque la RPC nueva.
