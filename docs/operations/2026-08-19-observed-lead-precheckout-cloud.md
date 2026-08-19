# Evidencia Cloud — receiver observado `lead.precheckout`

Fecha: 2026-08-19

Estado: **migración aplicada y verificada; receiver y efectos productivos no activados**.

## Alcance

Se integró el PR #48 mediante merge commit y se aplicó en Supabase Cloud únicamente:

```text
20260818000200_observed_lead_precheckout.sql
```

No se desplegó una nueva imagen del bridge, no se configuró el relay de la landing y no se activaron receiver, workers, dispatcher, follow-ups ni outbound.

## Provenance

- Merge commit remoto: `7450c6e012a3c4feda0fefb2a250aa0125158065`.
- Commit revisado contenido en el merge: `80c378888dbffa075e5d4c2a50e063d44a1a71e7`.
- SHA-256 de la migración aplicada: `fc21834ba66cef88e6495e4c9f31f12847f032647ed7eafc9a83998f887c7f92`.
- Supabase CLI fijado para el apply: `2.113.0`.
- PostgreSQL remoto observado: `17.6`.

## Preflight

- Tracking remoto terminal antes del apply: `20260818000100_precheckout_test_first_touch`.
- Dry-run remoto exacto: una sola migración pendiente, `20260818000200`.
- Runtime durable observado: `paused`, generación `9`.
- Counts antes del apply:
  - `precheckout_submissions=1`;
  - `purchase_intents=1`;
  - `purchase_intent_submissions=1`;
  - `precheckout_submission_conflicts=0`.
- No había intents `provider_observed`, con `activation_authorized` ni con `whatsapp_contact_authorized`.

## Apply

El CLI ejecutó `db push` sólo después de repetir el dry-run exacto y comprobar commit y hash fijados. Resultado:

```text
attempted=true
reported_success=true
reported_versions=[20260818000200]
exit_code=0
```

No se imprimieron ni persistieron credenciales en la evidencia.

## Postflight independiente

Supabase MCP read-only confirmó:

- tracking terminal `20260818000200_observed_lead_precheckout`;
- cola posterior vacía en un segundo `db push --dry-run`;
- función `admit_observed_lead_precheckout(text,jsonb,jsonb)`:
  - owner `postgres`;
  - `SECURITY DEFINER`;
  - `search_path=pg_catalog, public, pg_temp`;
  - `anon`: sin `EXECUTE`;
  - `authenticated`: sin `EXECUTE`;
  - `service_role`: con `EXECUTE`;
- `purchase_intents.normalized_phone` nullable con check E.164;
- índice único parcial `purchase_intents_one_observed_email_idx` presente;
- counts sin cambios respecto del preflight;
- `provider_observed_intents=0`;
- `activation_authorized_intents=0`;
- `whatsapp_authorized_intents=0`;
- runtime durable todavía `paused`, generación `9`.

## Advisors

Se reejecutaron advisors de seguridad y rendimiento después del DDL. La función nueva no apareció en los warnings de `search_path`.

Persisten advisories históricos, fuera del alcance de este apply:

- tablas con RLS habilitado sin políticas;
- tres funciones legacy con `search_path` mutable;
- foreign keys sin índice, índices sin uso y dos pares de índices duplicados.

Las cinco tablas precheckout sin RLS conservan ACL directas cerradas para `anon`, `authenticated` y `service_role`; el acceso de aplicación sigue mediado por funciones permitidas. No se habilitó RLS automáticamente porque hacerlo sin políticas podría romper el bridge.

## Resultado

La capacidad durable quedó instalada en Supabase Cloud, pero no está conectada a tráfico productivo. El siguiente gate separado es desplegar el merge commit con `LEAD_PRECHECKOUT_RECEIVER_ENABLED=false`, verificar provenance y salud, y recién después configurar el relay server-side con una activación supervisada independiente.
