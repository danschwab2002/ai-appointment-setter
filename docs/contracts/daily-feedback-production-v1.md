# Contrato productivo de reporte diario de feedback V1

- **Estado:** aceptado para implementación
- **Versión:** `daily-feedback-production-v1`
- **Autoridad inicial:** tenant, scope, conjunto cerrado de revisores, Slack team/users y ruta Slack son inputs explícitos de despliegue
- **Política temporal:** timezone IANA, cutoff local, retención, política y responsables de eliminación son inputs explícitos; no tienen defaults productivos

## Objetivo

Materializar una sola revisión diaria autoritativa desde Chatwoot, publicar un único acceso en el canal privado de Slack y conservar decisiones humanas sin modificar automáticamente conocimiento, prompts ni Conversation Releases.

## Ruta productiva

```text
Chatwoot canónico
→ collector/minimizador determinístico
→ batch y snapshots en Supabase Cloud
→ notificación idempotente en Slack
→ HTTPS + Slack OpenID Connect
→ sesión opaca HttpOnly
→ reautorización durable en cada request
→ decisión append-only
→ purga y tombstone al vencer la retención configurada
```

El store de filesystem y el HTML `distribution_ready=false` permanecen como cuarentena y oráculo de comportamiento; no forman parte de esta ruta.

## Identidades y autoridad

No se intercambian ni infieren estas identidades:

- `tenant_ref`: autoridad canónica explícita del aliado;
- `scope_ref`: scope canónico explícito del runtime;
- `reviewer_ref`: referencia explícita de cada revisor autorizado;
- `reviewer_binding_id`: UUID durable individual;
- `oidc_issuer`, `oidc_subject`, `slack_team_id` y `slack_user_id`: identidad canónica devuelta y ligada por Slack OpenID Connect;
- `batch_id`: UUID interno;
- `public_ref`: UUID aleatorio opaco, identificador y no credencial;
- `session_token`: secreto aleatorio de 256 bits, guardado sólo como SHA-256;
- `csrf_token`: HMAC derivado con separación de dominio del secreto de sesión, guardado sólo como SHA-256;
- `command_id`: UUID de idempotencia;
- `worker_owner`, `lease_generation` y `lease_expires_at`: autoridad temporal del scheduler.

`configure_daily_feedback_scope_v2` recibe el conjunto deseado completo de exactamente cuatro revisores, rechaza claves desconocidas y duplicados, exige que los cuatro sean responsables de eliminación y mantiene una fila por persona en `daily_feedback_reviewer_bindings`. Al confirmar un lote, `daily_feedback_batch_reviewer_bindings` captura de forma inmutable cada binding, su generación y su identidad Slack/OpenID exacta. Agregar o reemplazar una persona después no concede acceso retroactivo. Desactivar o reemplazar sólo su fila revoca únicamente sus sesiones, incluso si su cookie todavía no venció.

## Ventana diaria

`daily_feedback_schedules` define timezone IANA y cutoff local explícitos. El lote usa:

- inicio inicial: cutoff local del día lógico anterior; después, `last_completed_window_end`;
- fin: el menor entre el momento de ejecución y el cutoff local de la fecha lógica;
- una sola clave lógica `(tenant_ref, scope_ref, local_date)`; el fingerprint del paquete detecta cambios de selección o contenido bajo esa clave.

Una ejecución exacta reutiliza el lote. El mismo lote lógico con fingerprint distinto falla `logical_batch_conflict` sin mutación.

## Recolección y minimización

Se reutiliza `ChatwootDailyCollector` y su sanitizer cerrado `deterministic-redaction-v1`.

El proceso debe verificar antes de leer contenido:

- feature flag explícito;
- HTTPS sin credenciales en URL;
- account e inbox iguales al binding comercial;
- agent-bot ID positivo y binding exacto al inbox, verificado con el endpoint inbox-scoped de Chatwoot (la existencia account-wide no basta);
- llave HMAC de al menos 32 bytes;
- evidencia de cifrado de Supabase Cloud;
- retención entre 24 y 168 horas en la aplicación productiva;
- política de eliminación no vacía y los cuatro reviewers marcados como responsables;
- conjunto exacto de cuatro reviewer bindings activo y estable durante claim y commit.

Sólo entran mensajes públicos de prospecto y del agent bot configurado, con salidas en estado `sent`, `delivered` o `read`. Notas privadas, adjuntos, mensajes humanos, fallos de entrega y otros autores quedan fuera. Chatwoot conserva contenido y orden canónicos; el batch guarda únicamente el snapshot minimizado.

## Persistencia

Supabase contiene, como mínimo:

- `daily_feedback_reviewer_bindings`;
- `daily_feedback_batch_reviewer_bindings`;
- `daily_feedback_schedules`;
- `daily_feedback_batches`;
- `daily_feedback_items`;
- `daily_feedback_decisions`;
- `daily_feedback_oidc_states`;
- `daily_feedback_sessions`;
- `daily_feedback_workflow_commands`;
- `daily_feedback_purge_tombstones`.

Los intentos de recolección y sus leases/fencing se materializan en
`daily_feedback_schedules`. El estado, los intentos y los leases de notificación
se materializan en `daily_feedback_batches`; no existen ledgers de intentos
paralelos que puedan divergir de esas filas autoritativas.

Invariantes físicos:

- RLS habilitado;
- sin DML directo para `anon`, `authenticated` ni `service_role`;
- sólo RPCs `SECURITY DEFINER` con `search_path` fijo y `PUBLIC` revocado;
- lote y snapshots inmutables;
- mensajes almacenados sólo en JSON minimizado y con tamaño acotado;
- una posición y una conversación por lote;
- una decisión terminal por item;
- múltiples revisores autorizados por lote, con decisión colaborativa: la primera decisión terminal válida gana;
- feedback literal requerido sólo para `correct_with_feedback` y prohibido para las otras decisiones;
- máximo de un candidato por decisión con feedback; V1 crea cero candidatos;
- expiración bloquea lectura y escritura antes de la purga física;
- purga decide vencimiento con el reloj autoritativo de PostgreSQL —nunca con la fecha aportada por el caller—, exige un límite explícito entre 1 y 100, borra contenido, sesiones y mappings batch-scoped y deja únicamente un tombstone append-only sin transcript ni feedback, con las identidades Slack/OpenID y generaciones snapshoteadas de los responsables humanos, y el worker que ejecutó la purga en un campo separado;
- el retry de notificación sólo admite códigos cerrados en minúsculas y demoras explícitas entre 1 y 900 segundos; `delivery_unknown` requiere reconciliación y no entra en retry automático.

## Scheduler y recuperación

Un worker dentro del servicio dedicado `daily-feedback` ejecuta el mismo
`run_once()` usado por el disparo operativo manual.

`DAILY_FEEDBACK_SCHEDULER_ENABLED=false` impide iniciar el polling background y
persiste el schedule como `enabled=false`. Sólo el endpoint run-now autenticado usa
`force=true`; el RPC permite ese claim manual sobre un schedule deshabilitado después
de comprobar el conjunto exacto de cuatro revisores. Un worker normal usa `force=false`
y no puede reclamarlo.

1. Purga lotes vencidos.
2. Reclama un schedule vencido mediante `FOR UPDATE SKIP LOCKED`.
3. Recolecta fuera de la transacción.
4. Confirma el lote con owner, generación y lease vigentes.
5. Reclama notificaciones pendientes desde el estado durable del batch, con su
   lease, generación y contador de intentos.
6. Envía un `REV-001` exacto al Slack connector.
7. Finaliza la admisión o conserva `delivery_unknown` para reconciliación
   explícita, sin un segundo post ciego.

No se repite una recolección ya confirmada. Un crash después de confirmar el batch no pierde el envío porque la notificación se reclama desde el lote durable. Un timeout del conector no autoriza un comando diferente: se reintenta el mismo `event_id` y `dedupe_key`.

## Slack

`REV-001` es una plantilla cerrada “Reporte diario listo”. El comando transporta sólo:

- UUID de evento;
- dedupe hash;
- timestamp;
- `review_ref` UUID;
- cantidad de conversaciones;
- fecha de expiración.

El connector construye la URL desde un `review_base_url` HTTPS configurado por tenant y agrega el path `/daily-feedback/review/{review_ref}`. Rechaza username/password, query, fragment, HTTP público, tenant sin base URL y host no configurado. Slack recibe un solo mensaje por lote y no recibe transcripts ni feedback. Se desactiva el unfurl del enlace.

## Autenticación HTTPS

El `public_ref` no concede acceso. Sin sesión, la página ofrece “Continuar con Slack”.

1. `GET /daily-feedback/auth/slack/start?batch_ref={public_ref}` crea un `state` aleatorio, guarda sólo su hash con TTL de 10 minutos y redirige a Slack OIDC.
2. `GET /daily-feedback/auth/slack/callback` consume una vez el state, intercambia el code por HTTPS, consulta `openid.connect.userInfo` y valida el issuer fijo de Slack, el `sub` canónico y su correspondencia exacta con team/user.
3. El backend crea una sesión de máximo 8 horas y nunca posterior a la retención del batch.
4. El navegador recibe cookies `__Host-*` con `Secure`, `HttpOnly`, `SameSite=Lax` y `Path=/`.

Cada GET y POST consulta Supabase y vuelve a validar sesión, binding activo, generación snapshoteada en el lote, tenant, scope, batch comprometido y no vencido. No hay bearer duradero en query ni en path. OAuth `code/state` son transitorios y se consumen una vez.

## Interfaz y decisiones

La superficie primaria es **Operate**: una conversación por vez, navegación secuencial y estado visible sin dashboard ornamental.

- muestra sólo snapshot minimizado;
- `correct`, `correct_with_feedback`, `skip`;
- feedback entre 1 y 4000 caracteres sólo para `correct_with_feedback`;
- POST server-side con CSRF independiente de la cookie;
- `command_id` UUID por submit;
- decisión append-only e inmutable;
- redirect PRG después de aceptar;
- al completar, muestra conteos sin proponer cambios automáticos.

V1 preserva feedback literal y genera cero interpretaciones y cero candidatos. Una futura etapa podrá proponer como máximo un candidato y requerirá confirmación humana separada.

## Headers y privacidad

Todas las respuestas de revisión usan:

```text
Cache-Control: no-store, max-age=0
Pragma: no-cache
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Permissions-Policy: camera=(), microphone=(), geolocation=()
Content-Security-Policy: default-src 'none'; style-src 'self' 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'
```

No se incluyen PII, secrets, tokens, URLs originales, adjuntos, analytics, third-party JS, imágenes ni logs de contenido.

## Readiness

`/ready` falla cuando la función está habilitada y falta cualquiera de:

- Supabase;
- Chatwoot;
- pseudonymization key;
- evidencia de cifrado;
- Slack OIDC client ID/secret/team;
- origen público HTTPS exacto;
- Slack connector producer;
- conjunto de bindings activos comprobable y consistente con el snapshot del lote;
- scheduler sano y sin un `delivery_unknown` vencido sin resolución.

## Fuera de alcance V1

- aprendizaje automático;
- edición o activación de Conversation Releases;
- interpretación generativa;
- creación de tickets/incidentes;
- dashboards;
- descarga del transcript o del HTML;
- decisiones desde Slack;
- revisión independiente por persona, quorum o múltiples votos por ítem;
- recolección desde memoria de Hermes, Slack o `public.messages`.
