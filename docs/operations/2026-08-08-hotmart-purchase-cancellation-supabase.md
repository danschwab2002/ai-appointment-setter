# Postflight Supabase de cancelación por compra — 2026-08-08

## Alcance

Registrar evidencia sanitizada de la aplicación manual, el postflight remoto y
una prueba conductual transaccional de `PURCHASE_APPROVED`. Los fixtures se
ejecutaron dentro de un bloque que terminó con una excepción deliberada para
forzar rollback. No se llamó al worker y no se envió ningún mensaje.

## Fuente aplicada

- rama: `feat/lancemos-purchase-cancellation`;
- implementación principal: commit `353e35c`;
- cierre de permisos del trigger: commit `2aeb676`;
- migraciones:
  - `20260808000100_hotmart_purchase_approved.sql`;
  - `20260808000200_hotmart_purchase_ordering_guard.sql`;
  - `20260808000300_hotmart_purchase_ordering_guard_privileges.sql`.

Las migraciones fueron ejecutadas manualmente en SQL Editor mediante bundles
fijados a esos commits. `list_migrations` continúa vacío, por lo que esta
aplicación no quedó registrada en el historial administrado de migraciones de
Supabase. El repositorio conserva la fuente canónica y los commits aplicados.

## Postflight remoto

La consulta independiente mediante el MCP Supabase de solo lectura confirmó:

```text
purchase_rpc_present = true
ordering_guard_function_present = true
deferred_trigger_enabled = true
transaction_unique_index_present = true

apply_hotmart_purchase_approved:
  anon_can_execute = false
  authenticated_can_execute = false
  service_role_can_execute = true

stop_cart_recovery_for_known_purchase:
  anon_can_execute = false
  authenticated_can_execute = false
  service_role_can_execute = false
```

La función interna sigue habilitada como trigger diferido aunque no tenga una
superficie RPC ejecutable directamente.

## Prueba conductual remota con rollback

Se ejecutó una única sentencia PL/pgSQL sobre la instancia Supabase. Validó y
luego revirtió atómicamente:

```text
PURCHASE_REMOTE_ROLLBACK_PROBE_OK
direct=applied
replay=already_applied
inverse=cancelled
in_flight=delivery_unknown
```

La excepción final `P0001` fue el resultado esperado y la condición que forzó
el rollback. El primer intento del harness fue rechazado correctamente porque
su timestamp RPC conservaba microsegundos mientras el payload durable usaba
milisegundos (`purchase_rpc_payload_mismatch`). Un segundo intento incompleto no
llegó a parsearse por faltar el delimitador final. Ninguno dejó datos.

Después del probe aprobado, una consulta independiente confirmó:

```text
webhook_residue = 0
contact_residue = 0
policy_residue = 0
case_residue = 0
attempt_residue = 0
```

## Estado previo observado

Antes de una prueba de compra, la base contenía ocho eventos
`PURCHASE_OUT_OF_SHOPPING_CART` procesados y ningún evento
`PURCHASE_APPROVED`. Esta comprobación usó sólo conteos por estado/tipo y no
extrajo payloads ni PII.

## Advisors

El advisor de seguridad no reportó una exposición nueva de las funciones de
compra. Conserva avisos preexistentes de tablas con RLS habilitado y sin
políticas, más tres funciones históricas con `search_path` mutable. El advisor
de performance conserva avisos preexistentes de foreign keys sin índice e
índices duplicados/no utilizados. No se corrigieron dentro de esta vertical.

Referencias:

- [Database linter: RLS enabled without policy](https://supabase.com/docs/guides/database/database-linter?lint=0008_rls_enabled_no_policy)
- [Database linter: mutable function search path](https://supabase.com/docs/guides/database/database-linter?lint=0011_function_search_path_mutable)
- [Database linter: unindexed foreign keys](https://supabase.com/docs/guides/database/database-linter?lint=0001_unindexed_foreign_keys)
- [Database linter: duplicate indexes](https://supabase.com/docs/guides/database/database-linter?lint=0009_duplicate_index)

## Qué demuestra

- el DDL existe en la instancia remota;
- el índice de idempotencia semántica existe;
- la guarda de orden inverso está instalada y habilitada;
- los permisos efectivos de ambas funciones respetan la frontera prevista;
- el RPC aplica el cierre directo y su replay es idempotente;
- el orden compra→abandono cancela la recuperación recién planificada;
- una entrega ya iniciada conserva `delivery_unknown`.

## Qué no demuestra

- que el bridge desplegado contenga todavía el commit `353e35c`;
- que PostgREST invoque el RPC con el contrato esperado;
- que un webhook real de Hotmart cierre un caso;
- que se haya enviado o cancelado un WhatsApp en producción.

La capacidad está **migrada y verificada estructural y conductualmente en SQL
remoto**, pero sigue **pendiente de despliegue del bridge y E2E**.
