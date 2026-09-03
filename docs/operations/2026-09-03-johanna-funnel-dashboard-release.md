# Evidencia de release del dashboard Johanna

- **Estado:** Reporte invalidado; contención verificada
- **Fecha UTC:** 2026-09-03
- **Alcance:** merge, despliegue del RPC read-only y primer reporte live sanitario

## Versión desplegada

- PR: `#95`
- merge commit en `origin/main`: `6743716bceadbb8ba75ba6ead3c66944ab406f21`
- commit revisado contenido por `origin/main`: `0abc77671f3665101e4195864ca0faf817ec57e6`
- migración: `20260831000100_johanna_funnel_dashboard_read.sql`
- SHA-256 de la migración: `fce78d5b59072fe0d3dd0b2461855e08a5e360d65c8128f16450c1b912946beb`

## Preflight y apply

El CLI fijado fue `supabase@2.113.0`. Un proyecto CLI privado y descartable,
con el prefijo canónico hasta la migración objetivo, mostró como único pendiente
`20260831000100`. Inmediatamente antes del apply se verificó:

- worktree limpio y fijado al merge commit;
- hash de migración idéntico al revisado;
- `pilot_boundary=disabled` y `automation_state=default_off`;
- backlog elegible, reservas, requests iniciadas y resultados ambiguos en cero.

El apply terminó con exit `0` y registró únicamente la versión
`20260831000100`.

## Postflight independiente

La lectura independiente de Supabase Cloud verificó:

- ledger `20260831000100 / johanna_funnel_dashboard_read / 5 statements`;
- firma exacta
  `read_johanna_funnel_dashboard_v1(timestamp with time zone,integer)`;
- owner `postgres`, `SECURITY DEFINER`, volatilidad `STABLE`;
- `search_path=pg_catalog, public, pg_temp`;
- `EXECUTE` efectivo sólo para `service_role`; sin acceso para `PUBLIC`, `anon`
  ni `authenticated`;
- ningún grant directo de tablas a roles API sobre las tablas consultadas;
- dry-run final sin migraciones pendientes;
- flags y backlog permanecieron sin cambios tras el apply.

Los advisors conservaron avisos de esquema ajenos a este RPC; el RPC desplegado
no apareció entre los avisos de `search_path` mutable.

## Primer reporte live

Se generó una cohorte UTC de siete días mediante una única invocación al RPC. El
artifact HTML resultante:

- SHA-256: `448f9e10cc658a7f38038188a34fb39712060df7ebe9aa2efdbc070199913c20`;
- `Supabase Cloud: complete`;
- `Chatwoot: unavailable`, porque no se suministró configuración opcional de
  enlaces ni se consultó su API;
- 3 casos en la cohorte, 3 con conversación vinculada y 1 que requiere atención;
- cero coincidencias para email, JID, UUID completo, JWT, headers de autorización,
  claves, HOTTOK o contenido conversacional.

El artifact reside fuera de Git en `/opt/data/cache/`, conforme a la política de
PII y datos capturados.

## Contención posterior

Una revisión adversarial posterior reprodujo dos defectos de aislamiento: la
proyección podía incluir intents de otro tenant y podía incorporar hechos
posteriores al cutoff histórico. Por lo tanto, el reporte anterior no constituye
evidencia válida y su artifact fue eliminado.

La migración forward-only
`20260831000200_disable_johanna_funnel_dashboard_read.sql` fue integrada mediante
PR `#97` y merge commit `491ac471ca17d3d1c82778f0823b3307204324d9`.
No se debe volver a generar ni entregar un dashboard hasta implementar y probar
ambos invariantes.

## Postflight de contención

- SHA-256 de la migración:
  `e96359672780e312aaa286acd06e551ad7b86890e35f9c6c770023108c3f1094`;
- apply limitado a `20260831000200`, exit `0`;
- ledger `disable_johanna_funnel_dashboard_read / 4 statements`;
- `EXECUTE=false` para `service_role`, `authenticated`, `anon` y `PUBLIC`;
- invocación HTTP autenticada como `service_role`: `403` fail-closed;
- dry-run final sin migraciones pendientes;
- `pilot_boundary=disabled`, `automation_state=default_off` y backlog elegible
  en cero antes y después del apply.
