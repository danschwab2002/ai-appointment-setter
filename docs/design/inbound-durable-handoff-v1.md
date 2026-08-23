# Handoff durable para casos comerciales inbound v1

- **Estado:** Implementada localmente; pendiente de despliegue y activación
- **Alcance:** migración aditiva, RPC service-role-only, cliente Python y wiring
  inbound antes del reply
- **Fuera de alcance:** seed automático de Team/policy y cambios de interfaz

## Objetivo

Permitir que una decisión inbound `needs_human` detenga de forma durable un
`commercial_cases.case_kind = 'inbound_sales'` sin fabricar un
`recovery_cases`. PostgreSQL es la autoridad del stop y crea dos efectos de
proyección independientes (`assignment` y `private_note`) en la misma
transacción.

## Modelo implementado

### Aggregate del handoff

`human_handoff_requests` queda anclado siempre por `commercial_case_id`.

- Los registros históricos/cart recovery se backfillean 1:1:
  `commercial_case_id = recovery_case_id`.
- Cart recovery conserva ambos identificadores y debe referir el mismo UUID.
- Inbound usa `commercial_case_id` con `recovery_case_id IS NULL`.
- Un trigger físico verifica que la forma corresponda al `case_kind` real.
- La unicidad parcial de requests vivos pasa a estar definida por
  `commercial_case_id`.

La función legacy `request_human_handoff(...)` conserva firma y cuerpo. Su
INSERT, que no conoce la columna nueva, recibe el root 1:1 mediante el trigger
`BEFORE INSERT`; el harness legacy verifica que admission, replay, leases y ACL
siguen funcionando.

### Scope de projection policy

Una versión de `human_handoff_projection_policies` referencia exactamente uno
de estos scopes:

1. pilot scope (`scope_key`, `scope_version`), o
2. inbound scope (`inbound_scope_key`, `inbound_scope_version`).

El request captura el mismo tipo de scope, además de account, inbox,
conversation, Team y template version. La política publicada sigue siendo
inmutable salvo la desactivación `active=true -> false`; la protección incluye
ahora las columnas inbound.

### Transición inbound permitida

La protección física de `commercial_cases` admite una única transición nueva:

```text
case_kind=inbound_sales
status=active, automation_status=draft_only, version=N
                        ↓
status=paused, automation_status=disabled, version=N+1
```

Todos los identificadores, scope, producto/oferta, autoridad y timestamps de
creación permanecen inmutables. No se agrega transición de reactivación.

## RPC `request_inbound_human_handoff`

Entrada:

- `commercial_case_id`
- `command_key`
- `reason_code`
- `projection_policy_key`
- `projection_policy_version`
- `now`

El RPC:

1. valida parámetros y serializa el namespace de `command_key`;
2. devuelve replay sólo si todos los inputs durables coinciden, y genera
   `human_handoff_command_conflict` si cambian;
3. bloquea contact y commercial case;
4. exige `case_kind='inbound_sales'` y estado `active/draft_only`;
5. deriva y verifica admission, scope publicado, channel identity y canonical
   conversation; account/inbox/conversation no son caller-owned;
6. exige una projection policy activa ligada al mismo inbound scope;
7. pausa/deshabilita el caso y pausa la conversación;
8. crea el request con `requested_by='agent'`, sin `source_action_id` ni
   `source_attempt_id` falsos;
9. crea `assignment` y `private_note` atómicamente.

El claim RPC existente fue ampliado para resolver tanto aggregates legacy como
inbound sin cambiar su firma ni su contrato de salida. Para inbound exige root
`paused/disabled`, conversación canónica exacta y scope inbound publicado.

## Seguridad

- `request_inbound_human_handoff` es `SECURITY DEFINER` con `search_path`
  cerrado.
- `PUBLIC`, `anon` y `authenticated` no tienen `EXECUTE`.
- Sólo `service_role` recibe `EXECUTE`.
- Las tablas de policy/request continúan sin DML directo para API roles.
- No se crea policy, Team, scope ni dato de cliente en la migración.

## Cliente Python

`SupabaseClient.request_inbound_human_handoff(...)` usa únicamente el RPC
atómico y valida estrictamente:

- cardinalidad exactamente 1;
- outcome `requested | already_requested`;
- UUID válido del request;
- contadores enteros no negativos.

Una respuesta HTTP 200 mal formada se clasifica como
`SupabaseCommittedResponseError` porque la transacción podría haberse
confirmado.

## Prerequisitos operativos

Antes de activar cualquier caller inbound deben existir y verificarse:

1. un `inbound_commercial_scope_versions` publicado que corresponda al caso;
2. una versión de `human_handoff_projection_policies` activa ligada **sólo** a
   ese inbound scope;
3. un `expected_team_id` real y accesible en Chatwoot;
4. template/version y cuerpo de nota privada revisados;
5. worker de proyección habilitado y listo para drenar ambos efectos;
6. postflight de privilegios efectivos y prueba contra PostgreSQL/Supabase real.

La ausencia de scope, policy o Team válido falla cerrada; no hay fallback ni
seed implícito.

## Evidencia local

Se ejecutaron tests Python focalizados, el harness PGlite legacy completo de
handoff y un probe PGlite inbound sobre el stack ordenado. El probe verificó
stop, ausencia de recovery case sintético, dos efectos claimables, replay,
conflicto y ACL. Esta evidencia no prueba concurrencia multi-sesión ni
privilegios efectivos en Supabase remoto.

## Riesgos abiertos

- La serialización concurrente se diseñó con advisory lock + row locks, pero no
  fue probada con dos sesiones PostgreSQL reales.
- PGlite valida semántica local, no paridad total de roles/default grants de una
  instancia Supabase administrada.
- No hay policy/Team seed por diseño: activación prematura falla cerrada.
- La activación productiva depende de crear un Team real y publicar una policy
  ligada al scope inbound; su ausencia mantiene el flujo cerrado.
