# Evidencia — laboratorio disposable PostgreSQL 17

- **Fecha:** 2026-08-12
- **Commit base:** `d2ee101c36353145b27aaffa667599345f40d9c3`
- **Estado:** `pass`
- **Alcance:** clean install local y failure injection del Supabase CLI
- **Mutaciones remotas:** ninguna
- **Datos de aplicación:** ninguno

## Entorno

PostgreSQL `17.10` se ejecutó rootless desde paquetes Debian extraídos en un
prefijo temporal. No se instaló software global ni se usó Docker. El cluster, sus
dos bases y las migraciones sintéticas se eliminaron al terminar.

`scripts/bootstrap_postgres17_rootless.py` reprodujo la preparación desde cero:
seis paquetes explícitos, binarios completos, major 17 verificado y manifest
privado con hashes. Ese prefijo recién generado volvió a pasar el laboratorio.

## Resultado sanitizado

```text
postgres_version=17.10
canonical_migrations=17
fingerprints_present=17
acl_inventory_rows=101
api_execute_leaks=0
trigger_service_execute_leaks=0
service_entrypoints=27
supabase_cli_version=2.113.0
cli_failure_exit_nonzero=true
migration_before_failure_recorded=true
object_before_failure_present=true
failed_migration_recorded=false
failed_object_present=false
later_migration_recorded=false
later_object_present=false
status=pass
```

El control del CLI usó tres migraciones sintéticas en otra base disposable:
una válida, una que ejecuta `raise exception` y una posterior válida. Esto prueba
que `db push` `2.113.0` devolvió error, confirmó la migración anterior y no dejó
registrada ni aplicada la fallida ni la posterior.

## Artefactos privados

Los manifiestos exhaustivos se guardaron fuera de Git bajo `data/`/cache:

```text
expected_prefix_sha256=694487bdae2d1300b04b31992e1720edef69fe7bb6812498ca949f9771144e60
full_stack_sha256=7eff8e3b51f95fd7b04f9f3f03b1dafca6c4dd403b7e94b8546eb225134a8905
summary_sha256=487fcc1f2791753bd4d6c37481f398bed31757bcb23065467548ccd61be6986d
```

No se versionan porque contienen identidades y cuerpos catalogados, aunque no
contienen filas de aplicación. Los hashes permiten verificar el handoff privado.

## Gates cerrados

- `postgres17_disposable_not_executed` → cerrado;
- `supabase_cli_failure_mode_unproved` → cerrado.

## Gates todavía bloqueados

- `prefix_exact_equivalence_unproved`: falta exportar el mismo manifiesto desde
  el remoto mediante lectura de catálogos y comparar exactamente;
- `migration_tracking_empty`: no se autoriza repair;
- `production_ddl_not_authorized`;
- `postflight_not_applicable_before_deploy`;
- `runtime_must_remain_inactive`.

Esta evidencia no autoriza `migration repair`, `db push` remoto, DDL, deploy ni
activación.
