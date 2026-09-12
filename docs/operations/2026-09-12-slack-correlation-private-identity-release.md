# Release: identidad privada para correlaciones de Slack

- **Fecha:** 2026-09-12
- **Estado:** desplegado y migrado; validación visual final en Slack pendiente
- **Alcance:** Johanna, canal dedicado `C0C0YEACVT2`

## Artefactos integrados

- PR: `#130`
- Commit revisado: `776e135b036de187f196a6714d7a4af7ce1526bf`
- Merge en `main`: `ed0e69a35515cf2f99f702c6532999d8087e325c`
- Migración: `20260912000100_operator_correlation_private_identity.sql`
- SHA-256 de la migración: `3736bedd2b736e0d414f2dbe584ab8263fbbd162193f54b66d82d3fc93a51dec`
- CLI de aplicación: `supabase@2.113.0`

## Verificación previa

- Suite Python completa: aprobada.
- Suite Node/PGlite completa: aprobada.
- Verificador de lectura privada: aprobado.
- Verificador ACL: aprobado.
- `compileall`, `diff --check`, preflight y `validate-tree`: aprobados.
- Revisión independiente final: `APPROVE`, sin bloqueadores.
- CI de PR `verify`: `success`.

La revisión detectó y corrigió dos fugas de superficie antes del merge:

1. el backfill público usa exclusivamente la lectura enmascarada;
2. Client Copilot conserva el detalle enmascarado y la identidad completa queda en
   el path privado usado sólo por la interacción de Slack tras validar sesión,
   tenant, Team ID, Channel ID y operador.

## Despliegue y migración

- `infra/appointment-bridge`: redesplegado; `/health=200`, `/ready=200`.
- `infra/supportmagician-slack-connector`: redesplegado; `/health=200`, `/ready=200`.
- El nuevo path `/private-review` respondió `401` sin bearer, confirmando el
  despliegue y el cierre por defecto.
- Dry-run previo: único pendiente `20260912000100`.
- Apply: exit `0`.
- Dry-run posterior: cola vacía.
- Ledger remoto: termina en `20260912000100_operator_correlation_private_identity`.

Postflight de la función exacta:

- `SECURITY DEFINER=true`;
- `search_path=pg_catalog, public, pg_temp`;
- `service_role EXECUTE=true`;
- `PUBLIC`, `anon` y `authenticated`: sin `EXECUTE`;
- la sesión de inspección no privilegiada recibió `permission denied`, como se
  esperaba.

Los advisors no señalaron la nueva función. Los avisos existentes de RLS sin
políticas, tres funciones históricas con `search_path` mutable, índices sin uso y
dos índices duplicados son ajenos a esta migración, que no crea tablas ni índices.

## Estado funcional y pendiente controlado

- Las tarjetas públicas, listados, backfill y Client Copilot siguen enmascarados.
- El modal privado está preparado para mostrar email y teléfono completos de la
  compra y de cada candidato, sin persistirlos en SQLite, metadata de Slack,
  auditoría ni logs.
- Caso autorizado: `C-f60d707d` / `f60d707d-5df1-4f74-85b8-aea9a5e750ce`.
- El caso sintético existe pero actualmente tiene `candidate_count=0`. La sesión
  de base disponible es read-only, por lo que no se alteró el fixture para inventar
  un candidato.
- Pendiente: abrir `C-f60d707d` en Slack y confirmar visualmente la identidad
  completa de compra. Para verificar comparación compra/persona hará falta añadir
  un candidato sintético por un canal de escritura autorizado y volver a abrir el
  mismo caso.
- No se interactuó con casos reales ni se ejecutó el backfill de las 13 tarjetas.
- No se expusieron ni registraron secretos o PII completa.
