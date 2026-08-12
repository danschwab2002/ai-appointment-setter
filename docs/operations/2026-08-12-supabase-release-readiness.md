# Evidencia — readiness de release Supabase

- **Fecha:** 2026-08-12
- **Estado:** `blocked`
- **Mutaciones remotas:** ninguna
- **Datos de aplicación leídos:** ninguno; sólo catálogos y resúmenes administrados

## Freeze local

```text
commit_base=13e120f1553a17037c5227c13494346004fb254d
canonical_migrations_before_hardening=16
canonical_migrations_after_hardening=17
postgres_target_major=17
```

El claim ACL anterior estaba limpio, sin diff/PR propio y su HEAD ya era ancestro de
`main`; se cerró como stale y se eliminó sólo ese worktree.

## Foto remota read-only

```text
managed_migration_versions=0
public_tables=15
public_indexes=57
public_functions=21
required_opt_out_scope_handoff_objects_present=false
api_role_execute_leaks=5
trigger_service_execute_leaks=6
security_definer_search_path_missing=0
```

Esto confirma un prefijo estructural antiguo y tracking vacío. No prueba equivalencia
exacta del prefijo ni autoriza repair.

Advisors observados:

- seguridad: tablas con RLS sin policies y tres funciones con `search_path` mutable;
- performance: foreign keys sin índice, índices no usados y dos pares de índices
  duplicados.

Referencias oficiales del advisor:

- [RLS enabled without policy](https://supabase.com/docs/guides/database/database-linter?lint=0008_rls_enabled_no_policy)
- [Function search path mutable](https://supabase.com/docs/guides/database/database-linter?lint=0011_function_search_path_mutable)
- [Unindexed foreign keys](https://supabase.com/docs/guides/database/database-linter?lint=0001_unindexed_foreign_keys)
- [Unused index](https://supabase.com/docs/guides/database/database-linter?lint=0005_unused_index)
- [Duplicate index](https://supabase.com/docs/guides/database/database-linter?lint=0009_duplicate_index)

No se cambian índices/policies dentro de este release sin clasificación y scope
separados. RLS sin policy es coherente con tablas service-only, pero debe volver a
clasificarse postflight.

## Evidencia local ejecutada

```text
pglite_full_stack_acl=PASS
public_functions=65
service_entrypoints=27
api_role_execute_leaks=0
trigger_service_execute_leaks=0
focused_pytest=8_passed
```

La migración `20260812000100_supabase_function_acl_hardening.sql` hace inventario
dinámico de todas las funciones públicas, revoca `EXECUTE` de PUBLIC y roles API,
y restaura sólo 27 RPC exactos para `service_role`.

Docker está instalado pero el daemon no estaba disponible. Por eso no se declara
ejecutada la receta PostgreSQL 17 ni probado el failure mode del Supabase CLI.
PGlite no sustituye esos gates.

## Reasons del bloqueo

```text
prefix_exact_equivalence_unproved
migration_tracking_empty
supabase_cli_failure_mode_unproved
postgres17_disposable_not_executed
production_ddl_not_authorized
postflight_not_applicable_before_deploy
runtime_must_remain_inactive
```

## Resultado

El paquete queda preparado para revisión y una autorización productiva posterior.
No está listo para ejecutar hasta cerrar equivalencia, dry-run exacto y prueba
fallida en proyecto disposable.
