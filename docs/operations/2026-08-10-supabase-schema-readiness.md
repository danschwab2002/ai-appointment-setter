# Evidencia read-only — readiness del esquema Supabase

- **Fecha:** 2026-08-10
- **Estado:** inspección remota completada; reconciliación y DDL no ejecutados
- **Commit canónico comparado:** `5584a6a38feca508a5730639dd01e6228f1de3f4`
- **Alcance:** migration history, fingerprints de catálogo, ACL efectivas y advisors
- **Datos de negocio extraídos:** ninguno

## 1. Método

Se usó la conexión Supabase MCP en modo de lectura para:

- listar migration history;
- inventariar nombres de tablas públicas sin consultar sus filas;
- consultar `pg_proc`, `pg_trigger` y `pg_indexes`;
- verificar body markers mínimos de funciones reemplazadas;
- comprobar `has_function_privilege` para `anon`, `authenticated` y
  `service_role` en las tres funciones críticas existentes;
- consultar advisors de seguridad y performance.

No se ejecutaron `INSERT`, `UPDATE`, `DELETE`, DDL, RPC de negocio ni probes de
comportamiento. No se imprimieron payloads, contactos, mensajes, credenciales ni
identificadores externos.

## 2. Migration history

La API administrada devolvió:

```text
migrations = []
```

Esto confirma tracking vacío en esa superficie. No demuestra esquema vacío: el
catálogo remoto contiene el motor follow-up y la vertical inicial de compra.

## 3. Resumen del catálogo

```text
public_tables = 15
public_functions = 21
public_triggers = 16
public_indexes = 57
```

Las 15 tablas públicas reportadas tienen RLS habilitado. Esta observación no
reemplaza la verificación de grants por tabla ni implica que existan policies.

## 4. Fingerprints por migración

| Versión | Marcadores observados | Resultado |
|---|---:|---|
| `20260803000100` | 7/7 esperados | fingerprint_present |
| `20260804000100` | ACL interna 1/1 | fingerprint_present |
| `20260804000200` | función descendiente 1/1 | fingerprint_present |
| `20260805000100` | función/trigger 2/2 | fingerprint_present |
| `20260805000200` | body marker 1/1 | fingerprint_present |
| `20260805000300` | body markers 2/2 | fingerprint_present |
| `20260808000100` | RPC/índice 2/2 | fingerprint_present |
| `20260808000200` | función/trigger 2/2 | fingerprint_present |
| `20260808000300` | ACL trigger-only 1/1 | fingerprint_present |
| `20260808000400` | 0/4 | fingerprint_absent |
| `20260808000500` | 0/3 | fingerprint_absent |
| `20260809000100` | 0/3 | fingerprint_absent |
| `20260810000100` | 0/5 | fingerprint_absent |
| `20260810000200` | 0/3 | fingerprint_absent |
| `20260810000300` | 0/4 | fingerprint_absent |

Las consultas remotas que originaron esta tabla se ejecutaron por grupos. Luego se
consolidó el mismo contrato en `scripts/supabase_schema_inventory.sql`; el archivo
queda preparado para la próxima repetición, pero este registro no afirma que ese
archivo consolidado haya sido enviado como una única sentencia.

El query consolidado sí fue ejecutado contra PGlite después de aplicar baseline y
las 15 migraciones en orden, emulando los roles/default grants de Supabase. Devolvió:

```text
supabase_schema_inventory_clean_stack=OK rows=15
```

Esto valida sintaxis y fingerprints para un clean install; no sustituye su futura
ejecución consolidada contra Supabase.

### Body markers confirmados

- `plan_cart_recovery_with_identity` contiene la materialización de
  `contact_authorizations` derivada de Hotmart;
- `get_followup_chatwoot_context` usa la conversación por caso;
- `record_and_finalize_followup_acceptance` conserva el guard
  `case_conversation_mismatch`.

Los body markers reducen incertidumbre sobre las migraciones de reemplazo, pero no
son hashes del source SQL ni prueban su aplicación exacta.

## 5. ACL efectivas observadas

| Función | anon | authenticated | service_role | Contrato observado |
|---|---:|---:|---:|---|
| `_finalize_followup_delivery_attempt(...)` | false | false | false | helper interno cerrado |
| `stop_cart_recovery_for_known_purchase()` | false | false | false | trigger-only cerrado |
| `apply_hotmart_purchase_approved(...)` | false | false | true | RPC de servicio |

La función de trigger sigue pudiendo ser invocada por PostgreSQL aunque no tenga
`EXECUTE` directo para esos roles.

### Inventario exhaustivo

Se agregó `scripts/supabase_acl_inventory.sql`, que enumera todas las funciones
por firma y compara `anon`, `authenticated` y `service_role` con una allowlist
explícita de 23 RPC del stack completo.

Sobre el esquema remoto actual devolvió nueve diferencias contra el target:

- tres RPC legacy todavía ejecutables por `service_role`; la cola pendiente está
  diseñada para cerrar/reemplazar esas superficies;
- cinco trigger functions ejecutables por roles API;
- `protect_scheduled_action_identity()` todavía ejecutable por `service_role`.

Sobre un clean install de las 15 migraciones quedaron cinco leaks trigger-only:

```text
protect_published_followup_policy()
serialize_contact_authorization_write()
set_updated_at()
validate_recovery_case_channel_identity()
validate_resolution_attempt_identity()
```

Por lo tanto, incluso el stack Git del corte no supera todavía el postflight ACL
exhaustivo. Se necesita una migración forward-only de hardening posterior a D;
no se debe reparar tracking ni desplegar/activar mientras este gate siga abierto.

## 6. Objetos ausentes determinantes

Se confirmó ausencia conjunta de:

- `finalize_purchase_stopped_delivery_attempts` y ambos triggers de safety fences;
- `hotmart_purchase_semantic_conflicts` y `admit_hotmart_purchase_approved`;
- `contact_opt_out_events` y `apply_chatwoot_inbound_opt_out`;
- `pilot_scope_versions` y `authorize_lancemos_pilot_request_start`;
- `hotmart_cart_abandonment_semantic_conflicts` y
  `admit_hotmart_cart_abandonment`;
- `pilot_recovery_case_bindings` y `plan_lancemos_pilot_cart_recovery`.

Por lo tanto, el runtime integrado en Git no debe activarse contra este esquema.

## 7. Advisors

### Seguridad

El advisor conserva:

- `rls_enabled_no_policy` para las tablas públicas; nivel `INFO`;
- `function_search_path_mutable` para `set_updated_at`,
  `validate_recovery_case_channel_identity` y
  `validate_resolution_attempt_identity`; nivel `WARN`.

Referencias:

- [RLS enabled without policy](https://supabase.com/docs/guides/database/database-linter?lint=0008_rls_enabled_no_policy)
- [Function search path mutable](https://supabase.com/docs/guides/database/database-linter?lint=0011_function_search_path_mutable)

La ausencia de policies es consistente con tablas backend-only cerradas a roles
API, pero debe verificarse nuevamente mediante privilegios efectivos después de
aplicar el stack pendiente. Los tres `search_path` mutables pertenecen al baseline
histórico y no se remediaron durante esta inspección.

### Performance

Se conservaron avisos de:

- foreign keys sin índice;
- índices sin uso observado;
- dos pares de índices duplicados.

Referencias:

- [Unindexed foreign keys](https://supabase.com/docs/guides/database/database-linter?lint=0001_unindexed_foreign_keys)
- [Unused index](https://supabase.com/docs/guides/database/database-linter?lint=0005_unused_index)
- [Duplicate index](https://supabase.com/docs/guides/database/database-linter?lint=0009_duplicate_index)

No se eliminan índices durante una reconciliación de seguridad. Esos avisos deben
tratarse en una vertical de performance separada, después de observar carga real.

## 8. Veredicto

```text
schema_tracking = divergent_and_repair_blocked
schema_fingerprint = present_through_20260808000300
pending_tail_starts = 20260808000400
runtime_activation = NO-GO
full_migrator_replay = NO-GO
safe_next_step = exact_prefix_schema_comparison_then_acl_hardening
```

El inicio de la cola es válido únicamente para el commit comparado. Debe
recalcularse si se integra la migración de handoff u otra migración antes del
freeze de despliegue.

## 9. Límites

Esta inspección prueba existencia/ausencia estructural, tres ACL puntuales y el
inventario ACL exhaustivo actual. No prueba:

- que los nueve archivos iniciales se aplicaron exactamente;
- historial reparado;
- comportamiento remoto de las migraciones pendientes;
- bridge desplegado desde el commit comparado;
- runtime armado;
- webhook Hotmart real;
- mensajes WABA ni handoff físico.
