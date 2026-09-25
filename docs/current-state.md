# Estado actual del sistema

- **Tipo:** snapshot de estado operativo versionado. No es arquitectura ni contrato: describe lo observado en una fecha, con su grado de verificación.
- **Fecha de corte:** 2026-09-25, actualizado con el cambio de modelo del agente comercial, el SOUL desplegado y el pin de la imagen de Hermes. El bloque anterior es del 2026-09-20 (activacion de la respuesta inbound y su E2E); el anterior a ese, del 2026-09-19 (auditoria de ingreso). **Las filas que no llevan fecha del 2026-09-25 no se re-verificaron en esta pasada.**
- **Commit de referencia:** `origin/main` = `98fd848` (merge del PR #181).
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
| `infra_hermes` | **2026-09-25 14:59:45** | **`nousresearch/hermes-agent:v2026.8.31`** | sin healthcheck |
| `infra_chatwoot` | 2026-09-07 12:49:44 | `4c96ec530e5f` | sin healthcheck |
| `att1-production_att1-bridge-dark` | 2026-09-06 17:14:31 | `4db34117da0f` | healthy |
| `att1-production_att1-product-hermes` | 2026-09-07 14:37:44 | `92b9613a9dcb` | sin healthcheck |
| `att1-production_att1-agent-profile` | **sin contenedor** | — | réplicas 0/1 |

- `att1-production_att1-agent-profile` lleva 13 días en `Pending` con el error de Swarm `no suitable node (insufficient resources on 1 node)`. **Causa raíz confirmada el 2026-09-19:** el servicio declara una reserva de memoria de 281474976710656 bytes (256 TiB) y un límite de 844424930131968 (768 TiB) en un nodo de 23 GB. Los dos números son 256 y 768 multiplicados por 1024⁴: es un error de unidades introducido en la edición del 2026-09-07 12:40 UTC, la única del Swarm que declara reservas. Corrección en EasyPanel: 256 MB de reserva y 768 MB de límite, o vaciar ambos campos. **No se aplicó:** corregirla levanta el profile comercial de ATT1, que no tiene Conversation Release aprobada. Detalle en `operations/2026-09-19-claude-production-ingress-audit-v1.md` §7.
- También corren en el mismo Swarm: `infra_chatwoot-db`, `infra_chatwoot-redis`, `infra_chatwoot-sidekiq`, `infra_evolution-api` (+ `-db`, `-redis`), `infra_ig-auth`, `infra_ig-db`, `infra_terms`, `att1-production_att1-postgres`, `easypanel`, `easypanel-traefik`, y el stack `cascara_*` de otro proyecto. **Confirmado** por `docker service ls`.
- Tags mutables: `infra_appointment-bridge` usa `easypanel/infra/appointment-bridge:latest`. `infra_chatwoot` está fijado a `v4.13.0`. **Confirmado.**
- **`infra_hermes` dejó de usar un tag móvil el 2026-09-25**, después de que un redespliegue trajera la 0.21.5 y dejara al agente comercial 21 minutos sin atender (§9, incidente 14). Está fijado a `nousresearch/hermes-agent:v2026.8.31` (0.21.0, upstream `29112bef`). **Confirmado 2026-09-25 16:12 UTC.** No volver a `:latest` sin migrar antes a gateway único.

### Correspondencia código desplegado ↔ Git

- **Confirmado 2026-09-19 contra el artefacto:** el bridge corre el árbol `src/` del commit `108d2ee8` (merge del PR #154). Se comparó el hash de objeto Git de los 44 archivos de `/app/src` del contenedor contra los últimos 40 commits de `main`: coinciden 44 de 44 con `108d2ee8` y 43 de 44 con `origin/main`. La variable `GIT_SHA` del servicio declara ese mismo commit, de modo que declaración y artefacto concuerdan. **La única diferencia con `origin/main` es `src/bridge/chatwoot.py`**, por el commit `2c80d82` del PR #156: esa es toda la superficie de riesgo de un release desde `main`. El método está en `operations/appointment-bridge-release-runbook-v1.md` §4.
- **Reportado (PR #160):** el conector de Slack declara `6287c14` (merge del PR #155) y su `app.py` coincide con `main` en esa fecha.
- **Confirmado 2026-09-19 con el mismo método:** `infra_supportmagician-slack-connector` corre el árbol de `6287c14f` (merge del PR #155), 44 de 44 objetos, 2 archivos de diferencia con `main`; `infra_daily-feedback` corre el de `943e8cf0` (merge del PR #141, 2026-09-13), 41 de 41 objetos y **13 archivos de diferencia con `main`**, seis días de atraso. Las dos declaraciones de `GIT_SHA` coinciden con su artefacto.
- **No comprobado:** SHA desplegado de los servicios `att1-production_*`.
- **Confirmado 2026-09-19:** `/health` y `/ready` del bridge se consultaron desde dentro del contenedor y respondieron `200` (detalle en §2, bloque de flags). Para los demás servicios, el health de Docker sigue siendo el único dato directo.

### Flags de efectos (nombres, sin valores sensibles)

**Confirmado 2026-09-20, tras la activación de la respuesta inbound.** Los cinco gates del camino inbound quedaron en `true` y se leyeron **dentro del proceso** del contenedor nuevo (`env` del contenedor, no la definición del servicio): `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED`, `CHATWOOT_CUT_B_ADMISSION_ENABLED`, `CHATWOOT_CUT_B_AGENT_ENABLED`, `CHATWOOT_AUTOMATED_REPLIES_ENABLED` y `HUMAN_HANDOFF_ADMISSION_ENABLED`. Siguen en `false`: `CHATWOOT_STALLED_MONITOR_ENABLED`, `LANCEMOS_PILOT_BOUNDARY_ENABLED`, `HOTMART_PURCHASE_WORKER_ENABLED`, `DURABLE_DISPATCHER_ENABLED`, `DURABLE_OUTBOUND_ENABLED` y `CHATWOOT_REPLY_SPLITTER_ENABLED`. **`PAYMENT_LINK_ENABLED` paso a `true` el 2026-09-22 11:39Z** (Dan, EasyPanel; ver la fila del Payment Link mas abajo). `/health` devuelve `200 status=ok` y `/ready` devuelve `200 status=ready`. Los workers de pre-resolución y de proyección a Slack siguen habilitados.

El `automation_state=default_off` / `pilot_boundary=disabled` que `/ready` sigue informando **no describe el camino inbound**: cuelga de `LANCEMOS_PILOT_BOUNDARY_ENABLED=false`, que gobierna el outbound del dispatcher durable (`app.py:3888-3897`). Leer ese campo como "el sistema está apagado" fue parte de por qué el inbound cerrado pasó semanas sin detectarse.

⚠ **La definición del servicio en EasyPanel quedó desalineada a propósito:** las cinco se aplicaron con `docker service update --env-add` sobre la imagen en curso, para no arrastrar el deploy de `origin/main` en el mismo movimiento. En el panel, `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED` sigue en `false`. **Un deploy desde el panel apagaría la respuesta inbound y el contenedor levantaría igual, sin error** — el apagón sería silencioso. Cerrar esa deuda guardando la variable en el panel es requisito previo a cualquier release (ver `operations/appointment-bridge-release-runbook-v1.md`).

⚠ **La vía de abandono de carrito de Hotmart sí está encendida:** `JOHANNA_ABANDONMENT_HOTMART_AUTO_ENABLED=true` y `HOTMART_ABANDONMENT_TIMER_WORKER_ENABLED=true`. Es la única entrada de negocio activa, y es la que está fallando (§9, incidente 8).

Único contador distinto de cero en `/ready`: `precheckout_delayed_delivery_unknown=1`, estado persistido en Supabase, anterior a la medición y sin fechar.

## 3. Git: ramas, PRs y cola de integración

**Confirmado 2026-09-19 (GitHub):**

| PR | Título | Rama | Estado | Nota |
|---|---|---|---|---|
| #163 | test: suite SQL independiente de la zona horaria e instalador de client-copilot portable | `feat/claude-suite-determinism-v1` | abierto, mergeable | Solo pruebas y un script de instalación. Evidencia en `operations/2026-09-19-claude-suite-determinism-and-portability-v1.md`. |
| #161 | fix: stabilize correlation prepare timestamps (partial; blocked metadata) | `feat/codex-correlation-operations-v1` | abierto, **draft** | Migración `20260918000100` + regresión SQL. Bloqueado: dos archivos que necesita están reservados por el claim de daily-feedback (`scripts/supabase_schema_inventory.sql`, `tests/test_supabase_release_readiness.py`). Suite SQL no aprobada completa. |
| #160 | docs: record partial Appointment Setter operational verification | `feat/codex-appointment-operations-v1` | abierto | Solo evidencia operativa. |
| #158 | fix: validate checkout inbox before sending purchase links | `feat/codex-checkout-operations-v2` | abierto, mergeable | Fix real con regresión; 39 tests focales y suite canónica reportados en verde como integrador. **Candidato a primer merge.** |
| #138 | feat: enforce Johanna learning capture before review | `docs/johanna-documentation-operating-protocol-v1` | abierto | Reserva `AGENTS.md`, `docs/documentation-governance.md` y `scripts/agent_workspace.py`. No tocar desde otro claim. |
| #125 | docs: record Johanna product-content deployment | `docs/johanna-product-content-deployment-evidence-v1` | abierto | Evidencia; espera revisión. |
| #87 | docs: reconcile MVP runtime evidence | `docs/mvp-status-reconciliation` | abierto desde 2026-08-30 | **No comprobado** si sigue vigente; sin claim activo en el registro. |
| #60 | docs: record Johanna reset production E2E | `docs/johanna-reset-production-e2e` | abierto desde 2026-08-23 | Ídem. |
| #27 | test: validate Supabase release on disposable PostgreSQL 17 | `test/postgres17-disposable-release-lab` | abierto desde 2026-08-12 | Ídem. |

Mergeados el 2026-09-18, en orden: #152, #151, #153, #154, #155, #156, #157, #159. Mergeado el 2026-09-19: #162 (`b04ad14`).

**Cola de integración propuesta** (decide Dan): (1) #158 · (2) #160 · (3) #163 · (4) #161 cuando se resuelva la frontera con daily-feedback · (5) #125 y #138 según sus dueños · (6) revisar si #87, #60 y #27 siguen teniendo sentido o se cierran.

Los tres primeros son independientes entre sí y ninguno toca runtime salvo el #158, que agrega una línea. **Mergear no despliega**: se verificó que los merges del 18 y del 19 de septiembre no dispararon ninguna acción de EasyPanel. Cada merge libera además los paths reservados por su claim, que es lo que hoy bloquea trabajo nuevo: el #160 libera `src/bridge/app.py` y `src/bridge/config.py`, necesarios para la propuesta `design/hotmart-delivery-durability-v1.md`.

### Checkout canónico

- **Confirmado 2026-09-19:** el checkout de integración del VPS quedó al día en `b04ad14`, limpio. Ese día se recuperó de 10 commits de atraso. Regla: ningún claim nuevo se crea sin `fetch` previo.

## 4. Claims y worktrees

**Confirmado 2026-09-19** leyendo el registro (`status` desde dentro del contenedor y los JSON de claims): 38 worktrees, todos administrados, más `main` protegido. Ningún worktree `unmanaged`. Por estado al cierre del día: 5 `implementing` (4 ajenos y sucios, más el de la auditoría), 7 `review`, 17 `merged`, 9 `abandoned`.

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
| `claude-suite-determinism-v1` | review | no | `feat/claude-suite-determinism-v1` | `claude-code-andy-20260919` | PR #163. |
| `claude-production-ingress-audit-v1` | implementing | no | `docs/claude-production-ingress-audit-v1` | `claude-code-andy-20260919` | Esta actualización y la auditoría de ingreso. |

Reglas vigentes sobre estos claims:

- Los cuatro worktrees sucios **se preservan tal cual**: no se limpian, stashean, commitean ni adoptan desde otra sesión sin transferencia formal de ownership decidida por Dan.
- Los 17 `merged` y 9 `abandoned` son terminales; sus worktrees siguen en disco y su limpieza (`cleanup`) es una operación posterior del integrador, no urgente.
- **Diferencia con la auditoría del 18/09:** `ops/johanna-payment-link-live-e2e-v1` ya no figura sucio (su registro se recuperó y commiteó en el PR #158), y los claims de los PRs #154 y #155 ya están en `merged`.

## 5. Matriz funcional

| Funcionalidad | Implementada | Mergeada | Desplegada | Activada | Validada E2E | Fuente |
|---|---|---|---|---|---|---|
| WABA identifier nulo (PR #154) | sí | sí | sí (bridge `108d2ee`) | compatibilidad activa | caso productivo exacto no revalidado | Reportado |
| Monitor: ignorar mensajes de actividad (PR #156) | sí | sí | **no** (es el único archivo que difiere del desplegado) | — | no | **Confirmado** (comparación de objetos Git) |
| Monitor: cobertura worker/sender (PR #159) | sí | sí | no aplica (solo tests y docs) | — | — | Confirmado |
| Stalled Conversation Monitor | sí | sí | parcial (sin #156) | **no** (`CHATWOOT_STALLED_MONITOR_ENABLED=false`) | no | Reportado |
| Diagnóstico de interacciones Slack (PR #155) | sí | sí | sí (conector `6287c14`) | sí | observabilidad sí; no corrige el submit | Reportado |
| Correlación V3: recomendación + tarjeta Slack | sí | sí | sí | sí | sí, caso sintético | Reportado |
| Correlación V3: confirmación humana final | sí | sí | sí | parcial | **no**: error genérico y `resolution_count=0` | Reportado |
| Correlación: timestamps de `prepare` (PR #161) | parcial | no | no | no | regresión SQL local sí; suite SQL completa no | Confirmado (PR) |
| Checkout V2: guarda de inbox antes de enviar (PR #158) | sí | no | no | no | tests focales | Confirmado (PR) |
| **Ingreso de Hotmart (abandono de carrito)** | sí | sí | sí | **sí, es la única vía de negocio activa** | **no: 10 de 103 entregas aceptadas en 3 días** | **Confirmado** (log del proxy) |
| Ingreso de Chatwoot (webhook de cuenta) | sí | sí | sí | sí | 27 entregas 2xx en 3 días, sin fallas | **Confirmado** (log del proxy) |
| **Admisión inbound por scope (Cut B)** | sí | sí | sí | **sí, desde 2026-09-20 11:41 ART** | **sí**: un remitente distinto del `ALLOWED_WHATSAPP_JID` fue admitido y respondido | **Confirmado** (E2E de Dan) |
| **Respuesta automática del agente** | sí | sí | sí | **sí, desde 2026-09-20 11:41 ART** | **sí**, mismo caso | **Confirmado** (E2E de Dan) |
| Derivación a humano (`HUMAN_HANDOFF_ADMISSION`) | sí | sí | sí | **sí, desde 2026-09-20 11:41 ART** | **no**: activada, nadie la ejerció | Confirmado (flag) |
| Opt-out durable y pausa humana | sí | sí | sí | sí | **no**: activados, sin ejercer | Confirmado (flag) |
| Respuesta al backlog de conversaciones sin contestar | no aplica | — | — | **no**: nada barre el backlog | — | **Confirmado**: el único disparador inbound es el webhook `message_created` y el monitor de estancadas está en `false` |
| **Payment Link V2 Johanna** | sí | sí | sí | **sí, desde 2026-09-22 11:39Z** (`PAYMENT_LINK_ENABLED=true`) | **parcial**: 3 emisiones, **ninguna entregada con atribución**. La oferta resuelve por el intent del lead (migración `20260922000100`, aplicada 2026-09-22 13:41Z) y la atribución resolvió en **`full`** el 2026-09-23 02:39Z (migración `20260922000200`, aplicada 2026-09-22 22:55Z): emisión `01M362384RZJCBHRZ9RQM61NA4`, conv 110, `dropped_unsafe_fields = null`. **Las dos emisiones con oferta del lead quedaron en `reserved`** porque un humano estaba atendiendo y el bridge derivó | **Confirmado** (`docs/operations/2026-09-22-johanna-checkout-offer-by-lead-intent-release.md`, `docs/operations/2026-09-22-johanna-checkout-link-full-attribution-release.md`) |
| Daily feedback: contexto operacional V2 | en curso (worktree sucio r3) | no | no | no | no | Confirmado |
| Personalización por primer nombre Johanna | en curso (worktree sucio) | no | no | no | no | Confirmado |
| ATT1: runtime y Conversation Release | parcial (candidato inerte) | parcial | infra dark; `att1-agent-profile` sin réplica | **no** | no | Reportado + Confirmado (0/1) |
| Canal administrativo de Codex | en curso (worktree sucio) | no | daemon `codex-preflight.service` apagado y deshabilitado el 19/09 (archivos conservados) | discontinuado | — | Confirmado |

## 6. Diferencias entre Git y runtime

- **`profiles/client-copilot/SOUL.md` (Git) ≠ `SOUL.md` runtime del profile `client-copilot`.** Reportado 18/09: el runtime es el role de onboarding/copiloto (2026-08-14) y Git tiene el role de operador de correlación; existe además un profile runtime separado `client-copilot-correlation-review`, detenido. **No copiar el archivo de Git sobre el runtime.** Resolución pendiente de Dan: versionar cada role con su nombre real.
- **Profiles efectivos se copian por fuera de Git** y pueden quedar detrás. **`profiles/agente-comercial/SOUL.md` coincide con `origin/main` (`98fd848`) desde el 2026-09-25 15:39 UTC**, md5 `ba54b79cd2dd8a294c01fad5d6b37ec9`, copiado desde Git y no editado en el servidor; backup del anterior en `private-backups/SOUL.md.2026-09-25-153953`. Confirmado por md5 el 2026-09-25 16:12 UTC.
- **El modelo del agente comercial no está en Git:** vive en `config.yaml` del profile. Desde el 2026-09-25 15:42 UTC corre `openrouter / z-ai/glm-5.2` con fallback `openrouter / anthropic/claude-sonnet-4.6`; antes era `anthropic / claude-sonnet-4-6` por OAuth. **El fallback no se probó.** Evidencia y riesgo abierto (1 propuesta inválida en 37) en `operations/2026-09-25-agente-comercial-glm-5-2-release.md`. Confirmado.
- **Secretos, flags y configuración efectiva viven en EasyPanel y en el bind de Hermes**, no en Git. Reportado; por diseño.
- **Imágenes con tag `latest`** en el bridge: sin pin por digest, no hay trazabilidad automática commit → imagen → contenedor. Confirmado. Se puede reconstruir a mano comparando objetos Git (§2), y desde el 2026-09-19 existe además un alias `preserved-20260919` de las tres imágenes en producción como destino de rollback. **Hermes salió de esta lista el 2026-09-25** (§2).
- **La configuración de entrega de Chatwoot no coincide con las rutas del bridge:** el AgentBot apunta a una ruta inexistente (§9, incidente 9). Confirmado 2026-09-19.
- El **checkout canónico** quedó al día en `b04ad14` el 2026-09-19. Confirmado.

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
- Cierre formal del canal de Codex: el daemon `codex-preflight.service` quedó desactivado y deshabilitado el 19/09 a las 16:15 ART. Faltan cerrar las sesiones SSH de Codex Desktop (las cierra Dan desde su app) y conservar `/var/lib/codex-development/` y `/home/codex/` como archivo. La bandeja quedó con 18 pedidos sin respuesta de 102, a propósito.
- `att1-agent-profile`: la causa ya está (§2, error de unidades en la reserva). Falta decidir si el servicio debe seguir definido y, si sí, autorizar la corrección en EasyPanel sabiendo que levanta el profile comercial de ATT1 sin Conversation Release aprobada.
- **Nuevo 2026-09-19:** qué hacer con el ingreso de Hotmart (§9, incidente 8) y con la `outgoing_url` del AgentBot (incidente 9). Las dos son correcciones en producción.
- **Nuevo 2026-09-19:** si el servicio pasa a fijarse por digest en vez de `:latest`, que es lo que daría trazabilidad real de commit a contenedor.

**Juan** (autoridad final de ATT1): producto y UX, aprobación comercial y de la Conversation Release, aceptación de facts, FAQs, Brand Voice, ejemplos y límites.

**Marcela** (ATT1): materiales autorizados, oferta, precio, audiencia, país, idioma, límites sanitarios; datos candidatos de checkout, oferta y landing. El descuento del 10 % fue reportado como aprobado, pero template, variable y publicación siguen pendientes. Reportado.

**Operador humano:** confirmación de cada correlación ambigua; responsable y SLA del handoff humano de ATT1 sin cerrar. Reportado.

## 9. Incidentes y bloqueos abiertos

1. **Confirmación humana de correlación V3 falla** (error genérico, sin resolución). PR #155 agregó diagnóstico; causa raíz no demostrada. **Congelado por instrucción de Dan:** no reabrir sin autorización explícita. Reportado.
2. ✅ **RESUELTO 2026-09-25 — PR #161 desbloqueado.** Estaba trabado por `scripts/supabase_schema_inventory.sql` y `tests/test_supabase_release_readiness.py`, reservados por otra tarea. Los dos quedaron libres y se movieron al claim `codex-correlation-operations-v1`, que se reabrió a `implementing` para completarlo. **La frontera se negoció, no se ignoró el preflight.** El bug que el PR arregla sigue vivo en producción, medido el 2026-09-25: `prepare_operator_correlation_resolution` lee `clock_timestamp()` dos veces mientras la columna `prepared_at` toma su propio default y el trigger exige `expires_at = prepared_at + 10 minutes` exacto.
3. **Bridge desplegado sin el PR #156.** Cualquier verificación del monitor contra producción debe partir de un release nuevo desde un SHA verificado de `origin/main`. Reportado + Confirmado.
4. **`att1-agent-profile` en 0/1**, 13 días, por una reserva de memoria de 256 TiB cargada por error el 2026-09-07 (§2). Causa raíz confirmada 2026-09-19; corrección pendiente de decisión.
5. **Una consulta de inventario de EasyPanel devolvió campos sensibles al contexto de auditoría** (18/09). Las credenciales afectadas deben considerarse para rotación; no se reproducen. Reportado.
6. **Máscara ACL de `/opt/hermes`** anuló traversal para el usuario `codex` el 18/09; se reparó solo ese permiso. Reportado. Si el canal de Codex se cierra, esas ACL quedan como deuda a revisar.
7. **Repositorio público** (ver §8). Verificado el 2026-09-19 que EasyPanel construye descargando un archive de GitHub (`Download Github Archive` en el log de la acción) y que los directorios de código del panel no conservan `.git`; **no** se pudo determinar desde el host si esa descarga usa un token. Antes de pasar el repositorio a privado hay que mirarlo en el panel, y además el clon canónico del VPS hace `fetch` por HTTPS sin credenciales.
8. 🔴 **Hotmart entrega y el bridge rechaza nueve de cada diez eventos.** 103 entregas en 3,05 días, 10 aceptadas. La cadena es `503 webhook_persist_unavailable` en el primer intento y `401` en los reintentos. **Causa demostrada** reproduciendo la admisión con PGlite sobre las 74 migraciones: el 503 es un **rechazo de contrato determinista** (versión distinta de `2.0.0`, `offer.code` ausente, `product.id` como texto, `creation_date` como texto o sin dato de contacto), no una indisponibilidad, y la llamada es atómica, así que el evento no queda ni registrado. Ningún reintento puede entrar, y los reintentos además chocan contra la ventana de frescura de 300 s. Hotmart desactiva la configuración del webhook ante fallas sostenidas, así que está en riesgo el canal entero, que además es la única vía de negocio encendida. **Lo que falta:** leer un payload fallido real en el historial del panel de Hotmart (60 días) para saber qué condición se incumple. Propuesta: `design/hotmart-delivery-durability-v1.md`.
9. 🔴 **El AgentBot de Chatwoot entrega a una ruta inexistente.** El bot id 1 del inbox 9 tiene `outgoing_url` terminada en `/webhooks/chatwoot/agent-bot`, que el bridge no expone: 145 respuestas 404 en 3 días, de forma continua. El ingreso canónico (`/webhooks/chatwoot`, webhook de cuenta) funciona, así que no hay pérdida demostrada del camino principal. Corrección: vaciar esa URL, no redirigirla al webhook de cuenta (exige encabezados de firma que la entrega de AgentBot no envía). **Confirmado** (log del proxy y tablas de configuración de Chatwoot, 2026-09-19).
10. **Quince horas de caída no registradas, del 2026-09-16 23h al 2026-09-17 14h UTC:** 339 respuestas 502 repartidas en los tres webhooks, entre ellas 10 de `/webhooks/lead`, cada una un lead potencial. Incidente cerrado, causa no investigada (es anterior al contenedor actual). Lo relevante es que nadie se enteró: no hay alerta sobre el ingreso.
11. **No había destino de rollback.** Las tres imágenes construidas desde el repositorio usan tag móvil y el demonio no conservaba ninguna versión anterior, así que un despliegue malo no tenía a dónde volver. Mitigado el 2026-09-19 con un alias local `preserved-20260919` para las tres imágenes en producción, sin tocar ningún servicio. El arreglo de fondo (pin por digest) lo decide Dan.
12. ✅ **RESUELTO 2026-09-20 — el agente no respondía ninguna conversación porque el inbound estaba cerrado por configuración.** Encontrado el 2026-09-20 de madrugada: con `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED=false`, la admisión comparaba al remitente contra un `ALLOWED_WHATSAPP_JID` con **un solo número de prueba** y descartaba a todos los demás en milisegundos, sin razonar ni registrar (`{"status":"ignored","reason":"sender_not_allowed"}`); y aun admitido, `CHATWOOT_AUTOMATED_REPLIES_ENABLED=false` impedía el envío. **El outbound, en cambio, estaba encendido para cualquier lead real**: el sistema abría conversaciones que no podía sostener. Estado del inbox al encontrarlo: 69 conversaciones abiertas y 12 con el último mensaje del contacto sin respuesta, entre el 2026-09-10 y el 2026-09-20; ningún agente humano había respondido desde el 2026-09-12. **Corregido** activando los cinco gates (§2), verificado antes con una simulación local del arranque y después con un E2E desde un número distinto del permitido. **Lo que queda de este incidente:** las 12 conversaciones siguen sin respuesta y nada las va a despertar salvo que esa persona vuelva a escribir; y la deuda de la variable en el panel (§2).
13. ⚠ **Ningún contador ni endpoint reporta las admisiones rechazadas.** El rechazo por remitente se resuelve dentro del handler y devuelve `200` con un cuerpo de 50 bytes: para el proxy y para cualquier monitor de disponibilidad, es una entrega exitosa. Por eso el incidente 12 pudo durar semanas con todos los health en verde. **No corregido.** Un contador de `ignored` por motivo en `/ready`, o una alerta sobre la relación entrantes/respondidos del inbox, es lo que lo habría hecho visible.
14. ✅ **RESUELTO 2026-09-25 — un redespliegue con `:latest` dejó al agente comercial 21 minutos muerto.** A las 14:39 UTC, al cargar una variable nueva, `infra_hermes` bajó `nousresearch/hermes-agent:latest`, que ese día era la 0.21.5. Desde la 0.21.4 Hermes exige **un gateway por host** y el contenedor levanta cuatro servicios s6, uno por profile: ganó el del profile `default`, se declaró STANDALONE y los otros tres murieron al arrancar sin que s6 los reintentara. Ningún lead entró en la ventana (cero mensajes en el inbox 9 entre 14:30 y 15:00, verificado en la base de Chatwoot). **Corregido** fijando la imagen a `v2026.8.31` (§2); el agente volvió a `connected` a las 15:00. Evidencia en `operations/2026-09-25-agente-comercial-glm-5-2-release.md` §3. **Lo que queda:** migrar a 0.21.5 con `hermes gateway migrate --multiplex`, probándolo antes con un profile de prueba. ⚠ Durante veinte minutos `gateway_state.json` informó `running` con el pid de un proceso muerto: para saber si un gateway vive se mira `ps` y el log de s6 del profile, no ese archivo.
15. 🔴 **Una propuesta mal formada del agente deja a esa persona sin respuesta, y nada lo señala.** `invalid_agent_output` se persiste como resultado terminal: el bridge no vuelve a pedir la propuesta. Con el modelo nuevo el riesgo es medible: **1 de 37** llamadas a GLM 5.2 con este SOUL devolvió JSON roto, contra **0 de 91** de Sonnet 4.6 en producción. **No corregido en producción:** el PR #182 agrega un reintento con Idempotency-Key propia (el api_server replica el contenido de una key ya vista) y un campo `attempts` para poder medir la tasa real; está mergeable y sin desplegar.

## 10. Próxima tarea aprobada y trabajos congelados

El primer PR de Claude Code (`docs/current-state.md`, #162) quedó mergeado el 2026-09-19 y el circuito completo (claim en el VPS, edición y pruebas en el clon local, PR, sincronización del worktree y transición a `review`) está verificado de punta a punta. El orden que sigue, con autorización separada para cada efecto:

0. **Guardar `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED=true` en el panel de EasyPanel** (§2). Alcanza con guardar, sin deployar. Va primero porque hasta que no esté, cualquier deploy apaga la respuesta inbound **sin error visible**, y eso incluye a los deploys que piden los puntos 5 y 6 de esta misma lista.
1. **Decidir qué hacer con el ingreso de Hotmart** (§9, incidente 8). Es lo único que hoy está perdiendo eventos de negocio en producción. La causa está demostrada y la propuesta escrita; el primer paso, que no depende de código, es abrir el historial de envíos del panel de Hotmart y leer un payload fallido para saber qué condición del contrato se incumple.
2. Integrar la cola de PRs (#158, #160, #163), que además libera los paths reservados que bloquean trabajo nuevo.
3. Corregir la `outgoing_url` del AgentBot de Chatwoot (§9, incidente 9).
4. Destrabar el PR #161 (frontera con el claim de daily-feedback, que reserva dos archivos que ese PR necesita).
5. Release del bridge desde `origin/main`, con todos los gates apagados, según `operations/appointment-bridge-release-runbook-v1.md`. La superficie es un solo archivo (`src/bridge/chatwoot.py`, PR #156).
6. E2E en serie: monitor → links → correlaciones, cada uno con su lista de pasos humanos (inbound desde teléfono de prueba, confirmación en Slack, compra controlada en Hotmart) preparada antes de pedirlos.

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
- Auditoría de ingreso del 2026-09-19: `docs/operations/2026-09-19-claude-production-ingress-audit-v1.md` (log del proxy, comparación de objetos Git, `/ready`, tablas de configuración de Chatwoot, definición del servicio de ATT1).
- `docs/operations/appointment-bridge-release-runbook-v1.md` y `docs/operations/2026-09-19-claude-suite-determinism-and-portability-v1.md`.
- `/var/lib/codex-development/operations/README.md`, `workspaces.json` y `READY.json` en el host.
