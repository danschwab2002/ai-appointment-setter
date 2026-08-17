# Postflight remoto del hotfix ACL del purchase worker — 2026-08-17

## Alcance ejecutado

Se aplicó en Supabase Cloud únicamente
`20260814000100_hotmart_purchase_worker_table_acl.sql`, desde el merge
`4e1f447ce4862dcbb9c332d486e3432f03558de8`. No se aplicó Corte A ni se
activó runtime.

## Evidencia verde

- Migration tracking registra `20260814000100`.
- `apply_hotmart_purchase_approved(...)` quedó `SECURITY DEFINER`.
- `service_role` conserva `EXECUTE` sobre la RPC.
- `anon` y `authenticated` no tienen `EXECUTE`.
- `service_role` ya no tiene `UPDATE` directo sobre
  `followup_delivery_attempts`.
- `pilot_runtime_controls` no contiene ningún runtime `armed`.
- `commercial_cases` continúa ausente.

## Discrepancia detectada

El postflight exigía explícitamente
`search_path=pg_catalog, public, pg_temp`, pero el catálogo conservó el valor
previo `search_path=public, pg_temp`. La migración 140001 no contenía una
sentencia `SET search_path`; los tests verificaban `SECURITY DEFINER` y ACL,
pero no `proconfig`.

No se ejecutó rollback porque el cambio aplicado cerró DML directo y la
inspección remota confirmó que `PUBLIC`, `anon`, `authenticated` y
`service_role` no poseen `CREATE` sobre schema `public`; revertir habría
reabierto el privilegio directo. El owner de la función es `postgres`.

## Remediación y gate

`20260814000150_hotmart_purchase_worker_search_path.sql` fija el orden
explícito y cuenta con un probe que valida `proconfig`. Esta corrección está
propuesta localmente y no aplicada. El despliegue de
`20260816000100_commercial_case_root.sql` permanece bloqueado hasta que
14000150 se revise, despliegue y pase postflight remoto.
