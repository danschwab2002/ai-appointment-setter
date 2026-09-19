# Estado actual del sistema

- **Tipo:** snapshot de estado operativo versionado. No es arquitectura ni contrato: describe lo observado en una fecha, con su grado de verificación.
- **Fecha de corte:** 2026-09-19.
- **Commit de referencia:** `origin/main` = `5e9d1cb` (merge del PR #159, 2026-09-18 20:32 UTC).
- **Mantenimiento:** lo actualiza quien programa, en el mismo PR que cambie cualquier fila de la matriz. Se reemplaza el snapshot entero; la historia queda en Git.
- **Convención:** `Confirmado` = inspección directa en la fecha de corte · `Reportado` = tomado de la auditoría del profile `default` de Hermes del 2026-09-18 o de un documento operativo versionado, sin re-verificar · `No comprobado` = falta evidencia.

Este documento no contiene secretos, identificadores de personas, números de teléfono ni payloads. Los nombres de flags aparecen sin valores sensibles.

## 1. Quién programa y cómo

**Confirmado.** Desde el 2026-09-19 la programación del repositorio la hace Claude Code, operado por Dan desde su Mac. Reemplaza al profile `default` de Hermes como programador, y cierra el piloto de Codex del 2026-09-18.

Modelo de trabajo:

- El clon canónico de integración sigue siendo `/opt/hermes/projects/ai-appointment-setter-integration` en el host, visible como `/opt/data/projects/ai-appointment-setter-integration` dentro del contenedor `infra_hermes`. El registro de claims vive en su Git common dir (`.git/hermes-agent-coordination/claims/`) y solo se opera desde dentro del contenedor, porque los 36 worktrees están registrados con rutas `/opt/data/...`.
- Claude Code edita y prueba en un clon de trabajo en la Mac. Ese clon no es fuente de verdad: la fuente es `origin/main` y el registro de claims del VPS.
- Cada tarea tiene claim, rama y worktree en el clon canónico (creados con `scripts/agent_workspace.py`), la rama se publica desde la Mac, y el worktree del VPS se sincroniza al mismo commit antes de mover el claim a `review`.
- Ramas con prefijo de herramienta (`docs/claude-...`, `feat/claude-...`), como hizo Codex con `codex-...`, para que el repositorio diga quién y con qué herramienta se hizo cada cosa.
- Entrega por PR. Dan conserva merge, deploy, rollback, cambios de flags, migraciones remotas, activaciones y E2E con efectos externos.
- El profile `default` de Hermes conserva contexto de producto, verificación independiente y el runtime del producto. No se apaga ni se modifica como parte de este cambio.

## 2. Servicios en producción (host Contabo, Docker Swarm vía EasyPanel)

**Confirmado 2026-09-19** con `docker inspect` (inicio del contenedor, digest corto de la imagen, health de Docker):

| Servicio | Contenedor iniciado (UTC) | Imagen (digest corto) | Health Docker |
|---|---|---|---|
| `infra_appointment-bridge` | 2026-09-18 15:15:09 | `e6598d47375d` | healthy |
| `infra_supportmagician-slack-connector` | 2026-09-18 14:20:54 | `4392a90dc51f` | healthy |
| `infra_daily-feedback` | 2026-09-14 01:46:57 | `b8e60a55e28a` | sin healthcheck |
| `infra_hermes` | 2026-09-07 13:48:44 | `7f74fd03c790` | sin healthcheck |
| `infra_chatwoot` | 2026-09-07 12:49:44 | `4c96ec530e5f` | sin healthcheck |
| `att1-production_att1-bridge-dark` | 2026-09-06 17:14:31 | `4db34117da0f` | healthy |
| `att1-production_att1-product-hermes` | 2026-09-07 14:37:44 | `92b9613a9dcb` | sin healthcheck |
| `att1-production_att1-agent-profile` | **sin contenedor** | — | réplicas 0/1 |

- `att1-production_att1-agent-profile` lleva 12 días en `Pending` con el error de Swarm `no suitable node (insufficient resources on 1 node)`. **Confirmado.** No se investigó la causa (reservas de recursos del servicio vs. capacidad del nodo).
- También corren en el mismo Swarm: `infra_chatwoot-db`, `infra_chatwoot-redis`, `infra_chatwoot-sidekiq`, `infra_evolution-api` (+ `-db`, `-redis`), `infra_ig-auth`, `infra_ig-db`, `infra_terms`, `att1-production_att1-postgres`, `easypanel`, `easypanel-traefik`, y el stack `cascara_*` de otro proyecto. **Confirmado** por `docker service ls`.
- Tags mutables: `infra_appointment-bridge` usa `easypanel/infra/appointment-bridge:latest` e `infra_hermes` usa `nousresearch/hermes-agent:latest`. `infra_chatwoot` está fijado a `v4.13.0`. **Confirmado.**

### Correspondencia código desplegado ↔ Git

- **Reportado (PR #160, 2026-09-18 19:32 UTC):** el bridge declara el SHA `108d2ee` (merge del PR #154). Su `chatwoot.py` coincide con esa revisión y **difiere de `main`**. El contenedor arrancó a las 15:15 UTC y el PR #156 (`fix(chatwoot): ignore activity messages in stalled scan`) se mergeó a las 15:29 UTC: **el bridge desplegado no incluye el PR #156**. Tampoco incluye los tests del PR #159, que no cambian runtime.
- **Reportado (PR #160):** el conector de Slack declara `6287c14` (merge del PR #155) y su `app.py` coincide con `main` en esa fecha.
- **No comprobado:** SHA desplegado de `infra_daily-feedback` y de los servicios `att1-production_*`.
- **No comprobado el 2026-09-19:** los endpoints `/health` y `/ready` no se consultaron en esta fecha; el health de Docker es el único dato directo.

### Flags de efectos (nombres, sin valores sensibles)

**Reportado (PR #160, baseline 2026-09-18 19:21 UTC):** `CHATWOOT_STALLED_MONITOR_ENABLED`, `CHATWOOT_CUT_B_ADMISSION_ENABLED`, `CHATWOOT_CUT_B_AGENT_ENABLED`, `CHATWOOT_AUTOMATED_REPLIES_ENABLED` y `PAYMENT_LINK_ENABLED` estaban en `false`; el bridge reportaba `automation_state=default_off` en `/ready`. Los workers de pre-resolución de correlaciones y de proyección a Slack ya estaban habilitados antes de esa tarea. **No se re-verificaron el 2026-09-19.**

## 3. Git: ramas, PRs y cola de integración

**Confirmado 2026-09-19 (GitHub):**

| PR | Título | Rama | Estado | Nota |
|---|---|---|---|---|
| #161 | fix: stabilize correlation prepare timestamps (partial; blocked metadata) | `feat/codex-correlation-operations-v1` | abierto, **draft** | Migración `20260918000100` + regresión SQL. Bloqueado: dos archivos que necesita están reservados por el claim de daily-feedback (`scripts/supabase_schema_inventory.sql`, `tests/test_supabase_release_readiness.py`). Suite SQL no aprobada completa. |
| #160 | docs: record partial Appointment Setter operational verification | `feat/codex-appointment-operations-v1` | abierto | Solo evidencia operativa. |
| #158 | fix: validate checkout inbox before sending purchase links | `feat/codex-checkout-operations-v2` | abierto, mergeable | Fix real con regresión; 39 tests focales y suite canónica reportados en verde como integrador. **Candidato a primer merge.** |
| #138 | feat: enforce Johanna learning capture before review | `docs/johanna-documentation-operating-protocol-v1` | abierto | Reserva `AGENTS.md`, `docs/documentation-governance.md` y `scripts/agent_workspace.py`. No tocar desde otro claim. |
| #125 | docs: record Johanna product-content deployment | `docs/johanna-product-content-deployment-evidence-v1` | abierto | Evidencia; espera revisión. |
| #87 | docs: reconcile MVP runtime evidence | `docs/mvp-status-reconciliation` | abierto desde 2026-08-30 | **No comprobado** si sigue vigente; sin claim activo en el registro. |
| #60 | docs: record Johanna reset production E2E | `docs/johanna-reset-production-e2e` | abierto desde 2026-08-23 | Ídem. |
| #27 | test: validate Supabase release on disposable PostgreSQL 17 | `test/postgres17-disposable-release-lab` | abierto desde 2026-08-12 | Ídem. |

Mergeados el 2026-09-18, en orden: #152, #151, #153, #154, #155, #156, #157, #159.

**Cola de integración propuesta** (decide Dan): (1) #158 · (2) #160 · (3) #161 cuando se resuelva la frontera con daily-feedback · (4) #125 y #138 según sus dueños · (5) revisar si #87, #60 y #27 siguen teniendo sentido o se cierran.

### Checkout canónico

- **Confirmado 2026-09-19 14:00 ART:** el checkout de integración del VPS estaba en `main` = `8d3fcf8`, **10 commits detrás** de `origin/main` (`5e9d1cb`), limpio. La actualización con `git pull --ff-only` es una operación pendiente del operador; hasta que ocurra, ningún claim nuevo debe crearse desde ese checkout sin `fetch` previo.

## 4. Claims y worktrees

**Confirmado 2026-09-19** leyendo el registro (`status` desde dentro del contenedor y los JSON de claims): 36 worktrees, todos administrados. Por estado: 4 `implementing`, 5 `review`, 17 `merged`, 9 `abandoned`, más `main` protegido. Ningún worktree `unmanaged`.

### Activos

| Claim | Estado | Sucio | Rama | Owner registrado | Qué es |
|---|---|---|---|---|---|
| `att1-product-hermes-runtime` | implementing | **sí** | `feat/att1-product-hermes-runtime` | `hermes-desktop` | Runtime y profile de producto ATT1; sujeto a aprobaciones comerciales. |
| `daily-feedback-operational-context-v2-r3` | implementing | **sí** | `feat/daily-feedback-operational-context-v2-r3` | `hermes-session-20260909_151657_1b9e04` | Contexto operacional V2 de daily feedback: código, contratos, dos migraciones y tests. Reserva `scripts/supabase_schema_inventory.sql` y `tests/test_supabase_release_readiness.py`, que bloquean al PR #161. |
| `johanna-first-name-personalization-v1` | implementing | **sí** | `feat/johanna-first-name-personalization-v1` | `session-20260908-first-name` | Personalización por primer nombre; migración `20260908000200`. |
| `codex-admin-operations-v1` | implementing | **sí** | `codex/admin-operations-v1` | `codex-coordinator-01a0b5bf` | Canal administrativo de Codex (`deploy/codex-admin-operations.service`, `tools/codex_admin`). Trabajo del piloto discontinuado. |
| `codex-checkout-operations-v2` | review | no | `feat/codex-checkout-operations-v2` | `codex-coordinator-01a0b5bf` | PR #158. |
| `codex-correlation-operations-v1` | review | no | `feat/codex-correlation-operations-v1` | `codex-coordinator-01a0b5bf` | PR #161. |
| `codex-appointment-operations-v1` | review | no | `feat/codex-appointment-operations-v1` | `codex-coordinator-01a0b5bf` | PR #160. |
| `johanna-documentation-operating-protocol-v1` | review | no | `docs/johanna-documentation-operating-protocol-v1` | `hermes-session-20260913` | PR #138. |
| `johanna-product-content-deployment-evidence-v1` | review | no | `docs/johanna-product-content-deployment-evidence-v1` | `hermes-desktop-20260911-deployment-log` | PR #125. |

Reglas vigentes sobre estos claims:

- Los cuatro worktrees sucios **se preservan tal cual**: no se limpian, stashean, commitean ni adoptan desde otra sesión sin transferencia formal de ownership decidida por Dan.
- Los 17 `merged` y 9 `abandoned` son terminales; sus worktrees siguen en disco y su limpieza (`cleanup`) es una operación posterior del integrador, no urgente.
- **Diferencia con la auditoría del 18/09:** `ops/johanna-payment-link-live-e2e-v1` ya no figura sucio (su registro se recuperó y commiteó en el PR #158), y los claims de los PRs #154 y #155 ya están en `merged`.

## 5. Matriz funcional

| Funcionalidad | Implementada | Mergeada | Desplegada | Activada | Validada E2E | Fuente |
|---|---|---|---|---|---|---|
| WABA identifier nulo (PR #154) | sí | sí | sí (bridge `108d2ee`) | compatibilidad activa | caso productivo exacto no revalidado | Reportado |
| Monitor: ignorar mensajes de actividad (PR #156) | sí | sí | **no** (bridge arrancó antes del merge) | — | no | Reportado + Confirmado (hora de inicio) |
| Monitor: cobertura worker/sender (PR #159) | sí | sí | no aplica (solo tests y docs) | — | — | Confirmado |
| Stalled Conversation Monitor | sí | sí | parcial (sin #156) | **no** (`CHATWOOT_STALLED_MONITOR_ENABLED=false`) | no | Reportado |
| Diagnóstico de interacciones Slack (PR #155) | sí | sí | sí (conector `6287c14`) | sí | observabilidad sí; no corrige el submit | Reportado |
| Correlación V3: recomendación + tarjeta Slack | sí | sí | sí | sí | sí, caso sintético | Reportado |
| Correlación V3: confirmación humana final | sí | sí | sí | parcial | **no**: error genérico y `resolution_count=0` | Reportado |
| Correlación: timestamps de `prepare` (PR #161) | parcial | no | no | no | regresión SQL local sí; suite SQL completa no | Confirmado (PR) |
| Checkout V2: guarda de inbox antes de enviar (PR #158) | sí | no | no | no | tests focales | Confirmado (PR) |
| Payment Link V2 Johanna | sí | sí | probable, sin SHA fijado | **no** (`PAYMENT_LINK_ENABLED=false`) | no | Reportado |
| Daily feedback: contexto operacional V2 | en curso (worktree sucio r3) | no | no | no | no | Confirmado |
| Personalización por primer nombre Johanna | en curso (worktree sucio) | no | no | no | no | Confirmado |
| ATT1: runtime y Conversation Release | parcial (candidato inerte) | parcial | infra dark; `att1-agent-profile` sin réplica | **no** | no | Reportado + Confirmado (0/1) |
| Canal administrativo de Codex | en curso (worktree sucio) | no | daemon `codex-preflight.service` instalado en el host | discontinuado | — | Confirmado |

## 6. Diferencias entre Git y runtime

- **`profiles/client-copilot/SOUL.md` (Git) ≠ `SOUL.md` runtime del profile `client-copilot`.** Reportado 18/09: el runtime es el role de onboarding/copiloto (2026-08-14) y Git tiene el role de operador de correlación; existe además un profile runtime separado `client-copilot-correlation-review`, detenido. **No copiar el archivo de Git sobre el runtime.** Resolución pendiente de Dan: versionar cada role con su nombre real.
- **Profiles efectivos se copian por fuera de Git** y pueden quedar detrás; `profiles/agente-comercial/SOUL.md` coincidía con Git el 18/09. Reportado.
- **Secretos, flags y configuración efectiva viven en EasyPanel y en el bind de Hermes**, no en Git. Reportado; por diseño.
- **Imágenes con tag `latest`** en bridge y Hermes: sin pin por digest, no hay trazabilidad commit → imagen → contenedor. Confirmado.
- **Checkout canónico detrás del remoto** (ver §3). Confirmado.

## 7. Estado por aliada

### Johanna

**Reportado 18/09.** Bridge, conector de Slack y servicios base saludables (confirmado por health de Docker el 19/09). Automatización en `default_off`. WABA reemplazó a Evolution como canal canónico. Canal de Slack exclusivo, no compartible con ATT1. Payment Link V2 apagado y sin E2E. Personalización por primer nombre en worktree sucio. Correlación V3 recomienda y publica tarjeta, pero la confirmación humana final no cierra. Autorización de WhatsApp en precheckout explícita; sin aceptación no se contacta. Regla operativa: batching inbound de 30 segundos antes del split outbound.

### ATT1

**Reportado 18/09 + Confirmado 19/09.** Infraestructura aislada en dark/default-off (`att1-bridge-dark` healthy, `att1-product-hermes` corriendo, `att1-agent-profile` sin réplica por recursos). Candidato de profile inerte versionado en `profiles/att1/`. Sin Conversation Release aprobada; faltan aprobaciones de Juan y materiales ratificados por Marcela. No reutilizar identificadores ni canal de Slack de Johanna. Sin outbound autorizado. Fuentes de intención: `docs/design/att1-conversation-release-v1.md` y `docs/design/att1-commercial-information-approval-v1.md`; el worktree sucio puede ir más adelante que esos documentos.

## 8. Decisiones pendientes por persona

**Dan**

- Cola de integración (§3) y disposición de los PRs #87, #60 y #27.
- Disposición de los cuatro worktrees sucios (transferir ownership, pausar o abandonar) y de los 26 worktrees terminales.
- Resolución del drift de `client-copilot` (§6).
- Cuándo y bajo qué alcance se reabre el arreglo de la confirmación humana de Slack (hoy congelado).
- Visibilidad del repositorio en GitHub: **es público** (confirmado 19/09). Antes de pasarlo a privado hay que verificar cómo lee EasyPanel el repositorio para construir bridge, conector y daily feedback, y cómo hace `fetch` el clon del VPS; si alguno usa HTTPS sin credenciales, el cambio los rompe en el próximo redeploy.
- Cierre formal del canal de Codex: desactivar `codex-preflight.service`, cerrar las sesiones SSH de Codex Desktop, conservar `/var/lib/codex-development/` y `/home/codex/` como archivo. Al 19/09 el daemon seguía activo y habilitado y la bandeja tenía 18 pedidos sin respuesta de 102.
- Causa del `att1-agent-profile` en 0/1 y si debe seguir definido.

**Juan** (autoridad final de ATT1): producto y UX, aprobación comercial y de la Conversation Release, aceptación de facts, FAQs, Brand Voice, ejemplos y límites.

**Marcela** (ATT1): materiales autorizados, oferta, precio, audiencia, país, idioma, límites sanitarios; datos candidatos de checkout, oferta y landing. El descuento del 10 % fue reportado como aprobado, pero template, variable y publicación siguen pendientes. Reportado.

**Operador humano:** confirmación de cada correlación ambigua; responsable y SLA del handoff humano de ATT1 sin cerrar. Reportado.

## 9. Incidentes y bloqueos abiertos

1. **Confirmación humana de correlación V3 falla** (error genérico, sin resolución). PR #155 agregó diagnóstico; causa raíz no demostrada. **Congelado por instrucción de Dan:** no reabrir sin autorización explícita. Reportado.
2. **PR #161 bloqueado** por dos archivos reservados por `daily-feedback-operational-context-v2-r3`. Se resuelve negociando la frontera, no ignorando el preflight. Confirmado.
3. **Bridge desplegado sin el PR #156.** Cualquier verificación del monitor contra producción debe partir de un release nuevo desde un SHA verificado de `origin/main`. Reportado + Confirmado.
4. **`att1-agent-profile` en 0/1** por recursos insuficientes, 12 días. Confirmado.
5. **Una consulta de inventario de EasyPanel devolvió campos sensibles al contexto de auditoría** (18/09). Las credenciales afectadas deben considerarse para rotación; no se reproducen. Reportado.
6. **Máscara ACL de `/opt/hermes`** anuló traversal para el usuario `codex` el 18/09; se reparó solo ese permiso. Reportado. Si el canal de Codex se cierra, esas ACL quedan como deuda a revisar.
7. **Repositorio público** (ver §8).

## 10. Próxima tarea aprobada y trabajos congelados

**Aprobado por Dan el 2026-09-19:** este documento como primer PR de Claude Code (claim `claude-current-state-v1`, rama `docs/claude-current-state-v1`, único path `docs/current-state.md`), precedido de la actualización del checkout canónico. Después, retomar los tres frentes donde los dejó Codex, en este orden y con autorización separada para cada efecto:

1. Integrar el PR #158 (merge de Dan).
2. Destrabar el PR #161 (frontera con daily-feedback).
3. Release del bridge desde un SHA verificado de `origin/main`, con todos los gates apagados, verificando digest, `/health`, `/ready` y logs sanitizados.
4. E2E en serie: monitor → links → correlaciones, cada uno con su lista de pasos humanos (inbound desde teléfono de prueba, confirmación en Slack, compra controlada en Hotmart) preparada antes de pedirlos.

**Congelado, no tocar:** los cuatro worktrees sucios; los PRs #125 y #138 salvo por sus dueños; las ramas de PRs mergeados salvo para `cleanup`; la confirmación de Slack; los profiles activos y en particular el `SOUL.md` runtime de `client-copilot`; secretos, bases, volúmenes, EasyPanel, Supabase y canales reales; los clones históricos `/opt/hermes/projects/ai-appointment-setter` y `/opt/hermes/ai-appointment-setter` como fuente de integración.

## 11. Mapa de autoridad

| Área | Autoridad final |
|---|---|
| Producto, prioridad y alcance general | Dan |
| Merge, deploy, rollback y producción | Dan |
| Implementación técnica reversible dentro de alcance aprobado | Claude Code, sujeto a claim y revisión |
| Arquitectura que cambia una decisión aceptada | Dan, con propuesta técnica |
| ATT1: producto, UX, contenido y Conversation Release | Juan |
| ATT1: facts y materiales fuente | Marcela aporta y ratifica; Juan aprueba su uso |
| Correlación individual ambigua | Operador humano autorizado |
| Integración Git | Dan |
| Contexto histórico y verificación independiente | Profile `default` de Hermes |
| Ambigüedad no cubierta | Fail-closed y elevar a Dan |

## 12. Fuentes

- Inspección directa del 2026-09-19: `docker service ls`, `docker inspect`, `git status`/`git worktree list` en el checkout canónico, `scripts/agent_workspace.py status` dentro del contenedor, JSON del registro de claims, API de GitHub.
- Auditoría de solo lectura del profile `default` de Hermes, 2026-09-18 (`respuesta-auditoria-codex-vps-hermes.md` y `respuesta-preguntas-complementarias-transferencia-hermes-codex.md`, guardadas fuera del repositorio en `/opt/hermes/attachments/`).
- `docs/operations/codex-appointment-operations-v1.md` (PR #160) y `docs/operations/codex-remote-access-pilot-v1.md` (PR #157).
- `/var/lib/codex-development/operations/README.md`, `workspaces.json` y `READY.json` en el host.
