# Postflight remoto del hotfix ACL del purchase worker — 2026-08-17

## Alcance ejecutado

Se aplicaron en Supabase Cloud, como dos gates separados:

- `20260814000100_hotmart_purchase_worker_table_acl.sql`, desde el merge
  `4e1f447ce4862dcbb9c332d486e3432f03558de8`;
- `20260814000150_hotmart_purchase_worker_search_path.sql`, desde el merge
  `617a492c538b5c0bd9e6a8e59976d7c26ddadde9`.

No se aplicó Corte A ni se activó runtime.

## Evidencia verde

- Migration tracking registra `20260814000100`.
- Migration tracking registra `20260814000150`.
- `apply_hotmart_purchase_approved(...)` quedó `SECURITY DEFINER`.
- El owner continúa siendo `postgres` y el catálogo registra exactamente
  `search_path=pg_catalog, public, pg_temp`.
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

## Remediación cerrada y gate siguiente

`20260814000150_hotmart_purchase_worker_search_path.sql` fija el orden
explícito y cuenta con un probe que valida `proconfig`. La corrección se aplicó y
su postflight remoto confirmó tracking, owner, `prosecdef`, `proconfig`, ACL,
privilegios de schema, runtime no armado y ausencia de `commercial_cases`.

`20260816000100_commercial_case_root.sql` ya no está bloqueada por esta
frontera, pero continúa sin aplicar y sin autorización productiva. Requiere su
propio dry-run, preflight, permiso DDL y postflight.
