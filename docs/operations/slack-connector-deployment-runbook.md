# Despliegue del conector central de Slack en EasyPanel

- **Estado:** outbound Johanna desplegado; candidato interactivo local default-off y pendiente de activación controlada
- **Servicio:** `supportmagician-slack-connector`
- **Contrato:** [Slack Operations Connector V1](../contracts/slack-operations-connector-v1.md)
- **Backup/restore:** [Backup y restore del ledger](slack-connector-backup-restore.md)

## Invariantes obligatorios

- Desplegar sólo un **tag o revisión Git exacta** ya integrada y verificada; registrar el SHA completo y no usar una rama móvil.
- Política de actualización **Recreate/stop-first**, nunca rolling. Esperar cero contenedores anteriores antes de arrancar el nuevo.
- Exactamente **una réplica**, un worker Uvicorn y sin autoscaling.
- Toda recreación monta el **mismo volumen** persistente en `/app/data`. No crear un volumen nuevo ni borrar el anterior durante rollback.
- Dockerfile `deploy/slack-connector.Dockerfile`, puerto interno `8000`.
- Los flags y la activación comienzan inactivos. La presencia de secretos no autoriza efectos.

## 1. Arranque inerte y preflight de storage

Configurar placeholders sólo en el gestor privado:

```text
SLACK_INGRESS_ENABLED=false
SLACK_NOTIFICATIONS_ENABLED=false
SLACK_INTERACTIONS_ENABLED=false
SLACK_CORRELATION_BACKFILL_ENABLED=false
SLACK_CONNECTIVITY_CHECK_ENABLED=false
SLACK_STORAGE_PREFLIGHT_ENABLED=true
SLACK_ACTIVATION_MODE=inactive
SLACK_ACTIVATION_GENERATION=0
SLACK_STORAGE_PATH=/app/data/slack-connector.sqlite3
SLACK_OPERATOR_BEARER_TOKEN=<bearer operador distinto>
SLACK_TENANT_TOKENS_JSON={"johanna":"<bearer propio>"}
SLACK_BOT_TOKEN=<secreto sólo del conector>
SLACK_SIGNING_SECRET=<secreto de Basic Information; nunca en Git o chat>
SLACK_TEAM_ID=<Team ID exacto>
SLACK_TENANT_CHANNELS_JSON={"johanna":"C0C0YEACVT2"}
SLACK_TENANT_OPERATOR_USER_IDS_JSON={"johanna":["<Slack User ID autorizado>"]}
SLACK_TENANT_OPERATOR_BACKENDS_JSON={"johanna":{"base_url":"https://<bridge-host>","read_token":"<secreto distinto>","write_token":"<secreto distinto>"}}
```

Recrear stop-first y exigir:

```text
GET /health -> 200 status=ok
GET /ready  -> 200 mode=inactive storage_ready=true
ledger.pending=0
ledger.claimed=0
ledger.request_started=0
ledger.delivery_unknown=0
```

Cualquier conteo no cero detiene la activación: no borrar backlog para superar la barrera. El body de readiness sólo puede contener flags, modos, generaciones y conteos sanitizados.

## 2. Identidad Slack sin efectos

Mantener ingreso, notificaciones y activación inactivos. Cambiar sólo `SLACK_CONNECTIVITY_CHECK_ENABLED=true`, recrear stop-first con el mismo volumen y exigir `mode=connectivity_verified`. `connectivity_failed` bloquea el rollout.

## 3. Admisión durable, secuencial por tenant

Activar ingreso con outbound aún apagado:

```text
SLACK_INGRESS_ENABLED=true
SLACK_NOTIFICATIONS_ENABLED=false
SLACK_ACTIVATION_MODE=inactive
```

Para **Johanna**, emitir un único evento sintético sin PII y verificar secuencialmente: `202 pending`, replay exacto `200 duplicate`, conflicto `409`, aislamiento cross-tenant y cero mensajes Slack. ATT1 no tiene token autenticado ni ruta en este corte y debe responder `401`; no se incorpora hasta crear su canal exclusivo y agregar simultáneamente la misma clave a ambos mapas server-owned.

Antes de armar outbound exigir exactamente `pending=1`, `claimed=0`, `request_started=0`, `delivery_unknown=0` y cero mensajes físicos.

## 4. Activación controlada: exactamente un mensaje

La generación es durable y monotónica. Elegir una generación nueva `N` (mayor que la reportada por `/ready`) y configurar:

```text
SLACK_NOTIFICATIONS_ENABLED=true
SLACK_ACTIVATION_MODE=one_shot
SLACK_ACTIVATION_GENERATION=N
```

Recrear stop-first. El presupuesto durable de esa generación permite que `request_started` se confirme **una sola vez incluso tras reinicios**. Verificar exactamente un mensaje Johanna y su binding `channel_id=C0C0YEACVT2`/`message_ts`; el segundo evento debe seguir `pending`. Reiniciar una vez con la misma generación y demostrar que no aparece otro mensaje.

Si el resultado queda `delivery_unknown`, no reintentar ni cambiar de generación: usar el procedimiento de reconciliación del apartado 6.

Tras verificación humana del mensaje único, llamar con el bearer operador:

```http
POST /internal/v1/operator/verify-activation
Authorization: Bearer <operador>
Content-Type: application/json

{"generation":N}
```

Sólo una generación one-shot consumida puede quedar verificada.

## 5. Modo continuo sólo después de verificar

Elegir otra generación `N+1` y cambiar explícitamente:

```text
SLACK_ACTIVATION_MODE=continuous
SLACK_ACTIVATION_GENERATION=N+1
```

Recrear stop-first con una réplica y el mismo volumen. El arranque falla cerrado si la generación one-shot anterior no fue verificada. Confirmar `/ready.activation.mode=continuous` y luego procesar **secuencialmente**, primero el único pending restante y después un nuevo evento de un tenant; nunca abrir ambos productores simultáneamente durante la prueba inicial.

## 6. Bridge operator, backfill y activación interactiva

Este corte es separado del outbound. Mantener inicialmente el conector con `SLACK_INTERACTIONS_ENABLED=false` y el bridge con lectura y escritura operator apagadas. En `appointment-bridge`, cargar directamente en EasyPanel:

```text
OPERATOR_CORRELATION_READ_ENABLED=false
OPERATOR_CORRELATION_READ_TOKEN=<secreto de lectura>
OPERATOR_CORRELATION_TENANT_REF=lancemos
OPERATOR_CORRELATION_FUNNEL_REF=psicologajohanna
OPERATOR_CORRELATION_WRITE_ENABLED=false
OPERATOR_CORRELATION_WRITE_TOKEN=<secreto de escritura distinto>
OPERATOR_CORRELATION_ACTOR_REF=<actor fijo de compatibilidad>
OPERATOR_CORRELATION_ACTOR_PREFIX=slack
```

Los tokens read/write deben ser distintos y coincidir con los configurados en `SLACK_TENANT_OPERATOR_BACKENDS_JSON`. Primero desplegar ambos servicios default-off y exigir `GET /ready -> 200`; después activar `OPERATOR_CORRELATION_READ_ENABLED=true` y `OPERATOR_CORRELATION_WRITE_ENABLED=true` y comprobar con cada bearer únicamente su endpoint permitido. No habilitar Slack Interactivity si alguna identidad, scope o readiness falla.

### Backfill in-place de Johanna

1. Mantener `SLACK_INTERACTIONS_ENABLED=false` y configurar `SLACK_CORRELATION_BACKFILL_ENABLED=true`.
2. Recrear stop-first y exigir `/ready = 200`.
3. Invocar `POST /internal/v1/operator/backfill-correlation` con el bearer operador y `{"tenant_ref":"johanna"}` de forma secuencial.
4. Detenerse ante `rejected`, `delivery_unknown` o `pending`; no reintentar un resultado incierto.
5. Exigir exactamente **13 mensajes** originales actualizados mediante `chat.update`, cero `chat.postMessage`, los mismos `channel_id`/`message_ts`, trece respuestas `accepted` y luego una respuesta `idle`.
6. Volver inmediatamente a `SLACK_CORRELATION_BACKFILL_ENABLED=false` y recrear stop-first.

### Corte interactivo controlado

1. Confirmar `/ready = 200` en bridge y conector y backfill apagado.
2. Configurar `SLACK_INTERACTIONS_ENABLED=true`, recrear stop-first y exigir `interactions_enabled=true` en `/ready`.
3. Configurar en Slack la Request URL `https://infra-supportmagician-slack-connector.u5iqmf.easypanel.host/slack/interactions`, habilitar Interactivity y mantener Events API/Socket Mode apagados.
4. Con un operador allowlisted, resolver un solo caso controlado: **Revisar caso** → elección → confirmación; exigir actor `slack.<slack_user_id>`, resolución durable en Supabase Cloud y un único `chat.update` terminal sobre el root original.
5. Cancelar deja el caso pendiente; doble submit o evidencia obsoleta deben fallar cerrado.

### Rollback interactivo

1. Cambiar `SLACK_INTERACTIONS_ENABLED=false` y recrear stop-first; no cambiar `SLACK_NOTIFICATIONS_ENABLED` ni la generación outbound.
2. Deshabilitar Interactivity en Slack.
3. Mantener `OPERATOR_CORRELATION_READ_ENABLED=false` y `OPERATOR_CORRELATION_WRITE_ENABLED=false` si el bridge operator es la causa.
4. Inventariar jobs `request_started` o `delivery_unknown`; no reintentar ciegamente efectos externos.
5. El feed outbound Johanna continúa independiente y ATT1 permanece fail-closed hasta tener su canal exclusivo y fuente portable.

## 7. Reconciliación ejecutable de `delivery_unknown`

El bearer operador debe ser distinto de ambos bearers productores. Los productores reciben `401` en esta ruta y nunca pueden elegir tenant, canal, texto o mensaje.

Después de inspeccionar el canal exclusivo ligado durablemente al tenant, decidir una sola alternativa:

```json
{"decision":"confirm_delivered","tenant_ref":"johanna","notification_id":"<uuid>","message_ts":"1788800000.000001","thread_ts":null}
```

Esto liga evidencia al canal configurado server-side para ese tenant y deja `accepted`; o:

```json
{"decision":"confirm_not_delivered","tenant_ref":"johanna","notification_id":"<uuid>"}
```

Esto audita la decisión y vuelve a `pending`. Si pertenecía a la generación one-shot vigente, repone ese único presupuesto. No se aceptan `channel`, `text`, `message`, bloques ni claves extra. Un conflicto o evidencia de hilo inconsistente no muta el ledger.

## 8. Rollback y restore

1. Cerrar ingreso.
2. Mantener reconciliación disponible para requests ya iniciados; resolver o inventariar incertidumbre.
3. Apagar outbound/activación.
4. Detener el contenedor antes de recrear o restaurar.
5. Mantener una réplica, el mismo volumen y una imagen de revisión Git exacta compatible con el schema vigente.
6. Para restore seguir el runbook enlazado; nunca copiar el WAL activo.

Events API, Incoming Webhooks y Socket Mode permanecen apagados. Interactivity se habilita únicamente mediante el corte separado del contrato de resolución, después de desplegar el endpoint default-off y configurar firma, backend y allowlist.
