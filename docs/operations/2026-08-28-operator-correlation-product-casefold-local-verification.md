# Verificación local — normalización de producto en resolución de correlaciones

- **Fecha:** 2026-08-28
- **Estado:** verificación local PASS; no constituye evidencia de despliegue
- **Base:** `594daabb4a6cf819af08628b28979fcbb453541d`
- **Alcance:** `20260828000100_operator_correlation_product_casefold.sql`

## Hallazgo reproducido

La correlación Hotmart canónica aceptaba `product_ref` sin distinguir mayúsculas, pero la proyección y la resolución administrativa lo comparaban con igualdad exacta. Un candidato durable válido podía quedar fuera de `candidates`, mientras `candidate_count` seguía preservando el valor original. El flujo falló cerrado antes de preparar un comando.

La reproducción usa datos sintéticos y no contiene PII productiva.

## Corrección

La migración forward-only reemplaza, bajo firmas y conteos exactos, las comparaciones de producto en:

- validación del comando inmutable;
- preparación;
- confirmación;
- proyección de correlaciones pendientes.

Conserva igualdad exacta para tenant, funnel y oferta. No cambia firmas, owner, ACL, search path, tablas, evidencia determinística ni autorización de efectos.

## Evidencia ejecutada

```text
RED: case-folded candidate missing
GREEN: operator_correlation_manual_resolution=OK
GREEN: operator_correlation_manual_resolution_replay=OK
GREEN: operator_correlation_manual_resolution_stale_guard=OK
GREEN: operator_correlation_manual_resolution_owner_forgery_guard=OK
GREEN: operator_correlation_manual_resolution_zero_effects=OK
pytest focal: PASS
pytest completo: PASS
npm test full-stack SQL/ACL: PASS
ACL: 121 funciones públicas / 50 entrypoints service_role
validate-tree: 44 migraciones / 0 versiones duplicadas
git diff --check: PASS
```

## Límites

- No se aplicó esta migración a Supabase Cloud durante la verificación local.
- No se habilitó runtime administrativo.
- No se preparó ni confirmó ningún comando productivo.
- No se creó outbound, conversación ni mensaje.
