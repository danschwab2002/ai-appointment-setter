# Reconciliación de migraciones Supabase — piloto Lancemos

- **Estado:** Propuesta operativa preparada; sin DDL aplicado
- **Fecha de corte:** 2026-08-10
- **Base auditada:** `main` en `5584a6a38feca508a5730639dd01e6228f1de3f4`
- **Stack canónico del corte:** 15 migraciones
- **Dependencia concurrente:** el handoff ejecutable D prepara una migración posterior; debe integrarse antes de congelar el bundle final

## 1. Problema

El esquema remoto contiene objetos de las primeras verticales, pero la API de
migraciones administradas devuelve una lista vacía. Las migraciones de compra
`20260808000100..00300` fueron aplicadas manualmente y existen fingerprints de
migraciones anteriores. Reejecutar el stack completo suponiendo que el historial
vacío equivale a esquema vacío puede repetir DDL, fallar a mitad de camino o
reemplazar funciones sin una base de evidencia clara.

La reconciliación debe separar tres hechos:

1. **tracking:** qué versiones registra el mecanismo administrado;
2. **estructura:** qué objetos, cuerpos y ACL existen realmente;
3. **comportamiento:** qué transiciones fueron ejercitadas.

Un fingerprint presente no prueba que el archivo exacto haya sido aplicado y no
autoriza reparar tracking. Un historial registrado tampoco prueba que el objeto
remoto conserve el cuerpo o los privilegios esperados.

## 2. Foto remota read-only

La inspección del 2026-08-10 observó:

```text
managed_migration_versions = 0
public_tables = 15
public_functions = 21
public_triggers = 16
public_indexes = 57
```

Fingerprints presentes hasta `20260808000300`:

- motor follow-up y sus tablas/índices/RPC;
- hotfix de ACL del helper interno;
- binding de identidad;
- auditoría de identidad resuelta;
- autorización durable derivada del abandono;
- autoridad de conversación por caso;
- compra aprobada, orden inverso y ACL del trigger.

Fingerprints ausentes desde `20260808000400`:

- safety fences de compra;
- conflictos semánticos de compra;
- opt-out inbound durable;
- perímetro Lancemos;
- abandono Hotmart autoritativo;
- runtime del perímetro.

No se observaron fingerprints parciales dentro de esos dos grupos en las consultas
realizadas. La clasificación sigue siendo estructural, no una certificación de
que los nueve archivos iniciales fueron ejecutados byte por byte.

## 3. Clasificación del corte

| Versión | Fingerprint remoto | Tracking | Clasificación operativa |
|---|---|---|---|
| `20260803000100` | presente | ausente | estructura compatible; no reejecutar a ciegas |
| `20260804000100` | ACL efectiva presente | ausente | hotfix compatible; no reejecutar a ciegas |
| `20260804000200` | función descendiente presente | ausente | linaje compatible; archivo exacto no demostrable por catálogo |
| `20260805000100` | función + trigger presentes | ausente | estructura compatible |
| `20260805000200` | body marker de autorización presente | ausente | cuerpo compatible |
| `20260805000300` | body markers per-case presentes | ausente | cuerpo compatible |
| `20260808000100` | RPC + índice presentes | ausente | DDL aplicado y previamente probado con rollback |
| `20260808000200` | función + trigger presentes | ausente | DDL aplicado y previamente probado con rollback |
| `20260808000300` | ACL efectiva presente | ausente | DDL aplicado y ACL compatible |
| `20260808000400` | ausente | ausente | pendiente de aplicar |
| `20260808000500` | ausente | ausente | pendiente de aplicar |
| `20260809000100` | ausente | ausente | pendiente de aplicar |
| `20260810000100` | ausente | ausente | pendiente de aplicar |
| `20260810000200` | ausente | ausente | pendiente de aplicar |
| `20260810000300` | ausente | ausente | pendiente de aplicar |

La evidencia remota detallada queda en
[`2026-08-10-supabase-schema-readiness.md`](../operations/2026-08-10-supabase-schema-readiness.md).

## 4. Fuente canónica del corte

| Migración | SHA-256 |
|---|---|
| `20260803000100_followup_engine_v1.sql` | `a4b0c5ab22bc39ced8792dfb3345f8b4d00bb0725d5ca38b4e6a7c2115b482e3` |
| `20260804000100_followup_engine_permissions_hotfix.sql` | `26248279a0bfd453ba9c44924961c3ad7358b587a9ec917a5a9da6ca0305e1d3` |
| `20260804000200_followup_identity_binding.sql` | `bf86bdcf5fd8d455bb669bb10b154be3925ecebf4b7da899aa020bb2796a31eb` |
| `20260805000100_followup_identity_audit.sql` | `4247095bb9c1d3ec51e548c12cff49708c2a50cff1a933e03cc704d34ed74af1` |
| `20260805000200_followup_contact_authorization_grant.sql` | `65c8afd388132e7fd64a9477392b6a0fd8c7c0552eb947c68c2475aa87b7a838` |
| `20260805000300_per_case_conversation_anchor.sql` | `13cc51641e0721f23ca5833421b07633d0c072f25364810560e7e908eaf44671` |
| `20260808000100_hotmart_purchase_approved.sql` | `0da5339a3f8f02e4fa17e562dccd6bf9e3752edc99a27745d8ea8e2d7a6c94c1` |
| `20260808000200_hotmart_purchase_ordering_guard.sql` | `0970f80d3016b23c18e3bda1f06a1a5b9e71c9de9c3331b85c62ea295acdae55` |
| `20260808000300_hotmart_purchase_ordering_guard_privileges.sql` | `351bf6ec67ab2fc69840887bf5ef1072b9026da6cf1bd6d54a93e9833ad7b83e` |
| `20260808000400_hotmart_purchase_safety_fences.sql` | `a04b2ebb3a9e63b976b08e02224c280dd39f6aa8e9d0cd2406310b5cbb8f6409` |
| `20260808000500_hotmart_purchase_semantic_conflicts.sql` | `3d09d86e122203fd2c146123817d5aff82cbf03d082618481d4a8299531a1625` |
| `20260809000100_inbound_opt_out_durable.sql` | `ce27804e8ef2712a7141ae66b79c14c378924fbcf8914a0b3ac847631e36ca7d` |
| `20260810000100_lancemos_pilot_boundary.sql` | `3d325bb06ea802b6f8ffc318c061495ec2281ca8e80539e5fb2f1711990a68a9` |
| `20260810000200_hotmart_cart_abandonment_authoritative.sql` | `94831d1cd6d834054d53a8c5902580b21fe29099b67207fae440a470f12f0664` |
| `20260810000300_lancemos_pilot_boundary_runtime.sql` | `f363dc646e2fcef265756a554dddd153511334b43ec36ea827d8cc2f489d5e1c` |

Estos hashes caducan si `main` incorpora otra migración. El preflight final debe
regenerar el manifiesto desde el commit integrado que se vaya a desplegar.

## 5. Estrategia de reconciliación

### Fase A — congelar sin efectos

1. integrar D y cualquier migración aceptada antes del freeze;
2. fijar commit e image digest;
3. mantener apagados perímetro, workers y outbound;
4. ejecutar suite Python, PGlite y PostgreSQL disposable desde cero;
5. volver a ejecutar `scripts/supabase_schema_inventory.sql` remotamente;
6. exigir cero fingerprints parciales o inesperados.

### Fase B — demostrar equivalencia antes de reparar tracking

`scripts/supabase_schema_inventory.sql` es sólo un detector diagnóstico de
presencia/ausencia/partial. Sus 41 marcadores no bastan para declarar equivalencia
de las nueve migraciones históricas. La reparación permanece `blocked` hasta:

1. congelar un stack prefix `20260803000100..20260808000300` en PostgreSQL de la
   misma major version;
2. comparar exhaustivamente dump/manifest de esquema remoto contra ese prefix:
   tablas y relkind, columnas/orden/tipo/default/nullability, constraints, índices
   y definiciones, triggers con tabla/función/estado, RLS, firmas/cuerpos/atributos/
   `search_path` de funciones y grants efectivos;
3. resolver toda diferencia con una migración forward-only o clasificarla
   explícitamente como diferencia de plataforma no semántica;
4. repetir los probes conductuales históricos necesarios;
5. obtener revisión independiente del snapshot y de la comparación.

El dump remoto se obtiene fuera de Git y sin datos mediante Supabase CLI fijada:

```text
npx --yes supabase@2.113.0 db dump --linked --schema public --file <private-temp-path>
```

El dump canónico se genera desde una base disposable creada únicamente con
baseline + ese prefix. Ningún dump, URL o credencial se agrega al repositorio.

Sólo si la comparación resulta equivalente puede ejecutarse, con autorización de
producción, la reparación soportada oficialmente:

```text
npx --yes supabase@2.113.0 migration repair --linked --status applied \
  20260803000100 20260804000100 20260804000200 \
  20260805000100 20260805000200 20260805000300 \
  20260808000100 20260808000200 20260808000300
npx --yes supabase@2.113.0 migration list --linked
```

Según la documentación oficial, `migration repair` modifica sólo el tracking y
no aplica/revierte SQL. Registrar versión de CLI, output sanitizado y listado
posterior. Si la equivalencia no puede demostrarse, **no** reparar y **no**
reejecutar esos nueve archivos.

La reparación de tracking debe cambiar metadata de despliegue, no objetos de
negocio. Si el mecanismo disponible no permite distinguir reparación de aplicación
DDL, el gate queda `blocked` y se requiere un procedimiento manual revisado.

### Fase C — aplicar sólo la cola pendiente con el CLI fijado

1. volver a listar historial y fingerprints;
2. ejecutar `npx --yes supabase@2.113.0 db push --linked --dry-run` y exigir que
   liste exactamente la cola esperada, en orden y sin archivos históricos;
3. probar el mismo mecanismo/versión contra un proyecto disposable, incluida una
   migración con fallo inyectado, y demostrar que no aplica ni registra versiones
   posteriores;
4. ejecutar `npx --yes supabase@2.113.0 db push --linked` sólo después de esa
   prueba y de autorización productiva;
5. ejecutar `migration list`, fingerprint, ACL y advisors independientes.

Con el corte actual, la cola comienza en `20260808000400`, pero ese dato debe
recalcularse después de integrar D. Hasta probar el failure mode del CLI fijado,
la aplicación de la cola permanece `blocked`, no “preparada para ejecutar”.

Referencia oficial: [Supabase Database Migrations](https://supabase.com/docs/guides/deployment/database-migrations).

### Fase D — postflight antes de runtime

Exigir:

- historial y commit congelado con la misma lista ordenada;
- todos los fingerprints `present`;
- cero fingerprints parciales;
- `scripts/supabase_acl_inventory.sql` devuelve cada función pública por firma;
- cero `api_role_execute_leak`, `trigger_service_execute_leak`,
  `service_role_allowlist_mismatch` o `security_definer_search_path_missing`;
- advisors ejecutados y cambios clasificados;
- `/health` en receiver-only;
- runtime durable todavía `inactive` y outbound apagado.

El clean install actual todavía devuelve cinco leaks de funciones trigger-only;
por lo tanto se requiere, después de integrar D, una nueva migración de hardening
que cubra también cualquier función de D y deje el inventario en cero. Sólo después
se sigue la activación por etapas de los runbooks E/F.

## 6. Rollback

Estas migraciones son forward-only. El rollback operativo es:

1. cerrar ingreso;
2. pausar runtime durable antes de quitar consumidores;
3. drenar/reconciliar únicamente requests ya iniciados;
4. apagar outbound y workers en el orden válido;
5. no borrar tablas, triggers, ledgers ni filas para volver al esquema anterior;
6. corregir incompatibilidades con una migración posterior e inmutable;
7. repetir fingerprints, ACL y advisors antes de reactivar.

## 7. Gates `NO-GO`

- historial vacío o divergente sin reparación verificada;
- fingerprint parcial;
- objeto inesperado con el mismo nombre y cuerpo incompatible;
- leak efectivo de `EXECUTE` hacia `anon/authenticated`;
- helper interno o trigger-only invocable directamente;
- migración nueva en `main` después del freeze;
- plan de `db push --dry-run` sin commit/hashes o no probado desde cero;
- intento de ejecutar el migrador completo sobre el esquema parcialmente aplicado;
- cualquier fila no `ok` en el inventario ACL exhaustivo;
- runtime o cualquiera de `RESOLUTION_WORKER_ENABLED`,
  `HOTMART_PURCHASE_WORKER_ENABLED`, `DURABLE_DISPATCHER_ENABLED`,
  `DURABLE_OUTBOUND_ENABLED`, `CHATWOOT_DURABLE_OPT_OUT_ENABLED` o
  `LANCEMOS_PILOT_BOUNDARY_ENABLED` habilitado durante DDL/reconciliación.

## 8. Límites

Este diseño no aplica migraciones, no repara tracking, no prueba comportamiento
remoto nuevo y no despliega el bridge. Define el procedimiento seguro a ejecutar
cuando exista autorización explícita de producción.
