# Contrato del conector de operaciones Slack V1

- **Estado:** Aceptado, implementado y verificado; despliegue y activación pendientes
- **Fecha:** 2026-09-07
- **Servicio:** `supportmagician-slack-connector`
- **Canal fijo:** `C0C0YEACVT2`
- **Productores autorizados:** `johanna` y `att1`
- **Catálogo:** [Catálogo de mensajes operativos V1](../design/slack-operations-message-catalog-v1.md)

## 1. Frontera de confianza

El conector es el único runtime que posee `SLACK_BOT_TOKEN`. Los bridges no
reciben credenciales de Slack. Cada bridge se autentica ante el conector con un
bearer diferente; el conector deriva `tenant_ref` del bearer y nunca lo acepta
del cuerpo.

El caller no puede elegir canal, workspace, texto, bloques, severidad, título ni
metadata de Slack. Sólo admite un comando cerrado, y el servidor renderiza el
mensaje desde el catálogo versionado.

## 2. Configuración

| Variable | Requerida | Regla |
|---|---:|---|
| `SLACK_INGRESS_ENABLED` | sí | `true` habilita admisión autenticada |
| `SLACK_NOTIFICATIONS_ENABLED` | sí | `true` habilita el worker y `chat.postMessage` |
| `SLACK_INTERACTIONS_ENABLED` | sí | debe permanecer `false` en V1 |
| `SLACK_CONNECTIVITY_CHECK_ENABLED` | no | valida `auth.test` sin publicar |
| `SLACK_BOT_TOKEN` | para Slack | secreto; sólo token de bot `xoxb-…` |
| `SLACK_TEAM_ID` | para Slack | Team ID exacto esperado |
| `SLACK_CHANNEL_ID` | para Slack | Channel ID exacto; producción usa `C0C0YEACVT2` |
| `SLACK_TENANT_TOKENS_JSON` | para ingreso | objeto JSON con claves exactas `johanna` y `att1`; tokens distintos de al menos 32 caracteres |
| `SLACK_STORAGE_PATH` | para ingreso o salida | dentro del volumen persistente; default `/app/data/slack-connector.sqlite3` |
| `SLACK_WORKER_ID` | no | identidad opaca del único worker |
| `SLACK_POLL_INTERVAL_SECONDS` | no | entre `1` y `60` segundos; también limita la tasa por canal |
| `SLACK_MAX_NONTERMINAL_NOTIFICATIONS` | no | capacidad durable entre `1` y `100000`; cuenta `pending`, `claimed`, `request_started` y `delivery_unknown`; default `10000` |
| `SLACK_STORAGE_PREFLIGHT_ENABLED` | despliegue | `true` valida/abre el volumen aun con ingreso y efectos apagados |
| `SLACK_ACTIVATION_MODE` | salida | `inactive`, `one_shot` o `continuous` |
| `SLACK_ACTIVATION_GENERATION` | salida | entero durable monotónico; positivo fuera de `inactive` |
| `SLACK_OPERATOR_BEARER_TOKEN` | reconciliación | secreto distinto de ambos bearers productores |

Los booleanos sólo aceptan `true` o `false`. Una combinación incompleta impide
arrancar. La presencia de credenciales no habilita efectos.

## 3. Admisión

### `POST /internal/v1/notifications`

Cabecera obligatoria:

```http
Authorization: Bearer <credencial exclusiva del bridge>
Content-Type: application/json
```

El bearer se valida antes de leer el cuerpo. El cuerpo máximo es 8192 bytes y
sólo admite estas claves:

```json
{
  "event_id": "11111111-1111-4111-8111-111111111111",
  "event_code": "HND-001",
  "dedupe_key": "<sha256 hexadecimal de 64 caracteres>",
  "occurred_at": "2026-09-07T22:00:00Z",
  "subject_ref": "C-1A2B3C4D",
  "reason_code": "manual_handoff_required",
  "component": "chatwoot_projection",
  "state": "pending",
  "count": 1,
  "deadline_at": "2026-09-08T22:00:00Z"
}
```

Requeridos: `event_id`, `event_code`, `dedupe_key` y `occurred_at`. Los demás
son opcionales. No se aceptan nombres, teléfonos, emails, JIDs, texto libre,
URLs, payloads del proveedor, canal, tenant ni bloques Slack.

- `event_code` debe pertenecer a las 49 plantillas V1.
- `dedupe_key` es el SHA-256 de la semántica estable del evento, calculado por el
  productor sin incorporar PII en claro.
- `subject_ref`, cuando existe, usa el formato opaco `C-…`; define el hilo dentro
  del tenant.
- códigos de motivo, componente y estado son identificadores machine-readable,
  no copy libre.

Respuestas:

| HTTP | Resultado |
|---:|---|
| `202` | admitido y persistido como `pending` |
| `200` | replay exacto; no crea ni publica otro aviso |
| `400` | esquema o valor inválido |
| `401` | bearer ausente o inválido |
| `404` | ingreso deshabilitado |
| `409` | `event_id` o dedupe reutilizado con semántica distinta |
| `413` | cuerpo demasiado grande |

La respuesta no incluye el payload ni secretos.

### `GET /internal/v1/notifications/{event_id}`

Usa el mismo bearer. Sólo devuelve una notificación del tenant derivado. Expone
estado, código de evento y, tras aceptación, la referencia Slack
`channel_id/message_ts/thread_ts`. Un evento de otro tenant se comporta como
`404`.

## 4. Persistencia y estados

SQLite vive exclusivamente en el volumen persistente. El servicio usa WAL,
`synchronous=FULL`, transacciones `BEGIN IMMEDIATE`, constraints únicas y un
lock de instancia del sistema operativo.

```text
pending
  → claimed
  → request_started
      → accepted
      → rejected
      → delivery_unknown
```

- El ack `202` ocurre después del commit durable.
- Un replay exacto reutiliza el registro existente.
- `(tenant_ref,event_id)` y `(tenant_ref,event_code,dedupe_key)` son únicos.
- El worker persiste `request_started` antes de llamar a Slack.
- Una caída antes de `request_started` vuelve a `pending` al reiniciar.
- Una caída, cancelación, timeout, error de transporte o respuesta no verificable
  después de `request_started` termina en `delivery_unknown` y nunca se reintenta
  automáticamente.
- Cualquier `delivery_unknown` durable detiene globalmente el worker, incluso tras
  restart o revalidación de `auth.test`; sólo una reconciliación explícita puede
  desbloquear la salida.
- Un rechazo explícito `ok=false` de Slack termina en `rejected`; tampoco se
  reintenta automáticamente.
- `accepted` exige canal y `ts` exactamente válidos y se finaliza junto con el
  binding durable del hilo.
- El primer `subject_ref` aceptado crea la raíz `(tenant_ref,subject_ref)`; los
  siguientes mensajes del mismo tenant/objeto usan ese `thread_ts`.

El despliegue debe tener **exactamente una réplica y un worker Uvicorn**. Una
segunda instancia que comparta el volumen queda `not_ready`. No se admite
escalar horizontalmente esta implementación SQLite.

## 5. Render y privacidad

`src/slack_correlation/catalog.py` contiene las 49 entradas cerradas: severidad,
título y familia. El mensaje sólo muestra la etiqueta server-owned del tenant,
referencia opaca, códigos cerrados, conteos y timestamps UTC. No admite copy ni
bloques elegidos por el caller.

## 6. Operación

- `GET /health` prueba vida del proceso.
- `GET /ready` es `200` sólo si la combinación activa tiene almacenamiento,
  identidad Slack verificada y worker corriendo.
- `ledger` expone exclusivamente los conteos `pending`, `claimed`,
  `request_started` y `delivery_unknown`; no expone payloads ni identidades.
- `activation` expone modo, generación, presupuesto, consumo y verificación.
- `mode=inactive`: sin ingreso ni efectos.
- `mode=admission_only`: persiste, pero no publica.
- `mode=connectivity_verified`: `auth.test` coincidió; no implica publicación.
- `mode=operational`: ingreso/salida configurados y worker activo.
- `mode=storage_unavailable` o `connectivity_failed`: HTTP `503`.

Los logs no contienen cuerpos, credenciales, firmas ni errores crudos de Slack.

## 7. Activación y reconciliación operator-only

`one_shot` asigna presupuesto durable `1` a una generación. El consumo ocurre en
la misma transacción que `request_started`, por lo que un restart no repone el
presupuesto. Reutilizar una generación con otro modo o retrocederla falla
cerrado. Una generación nueva también se rechaza mientras exista cualquier
`claimed`, `request_started` o `delivery_unknown`. `continuous` sólo acepta una generación nueva después de que el operador
haya verificado una generación `one_shot` consumida mediante
`POST /internal/v1/operator/verify-activation`.

`POST /internal/v1/operator/reconcile-delivery` exige el bearer operador y un
schema cerrado. `confirm_delivered` requiere `tenant_ref`, UUID,
`message_ts` y `thread_ts` opcional; el canal proviene exclusivamente de
`SLACK_CHANNEL_ID`. `confirm_not_delivered` no acepta evidencia Slack, audita la
decisión y reencola de forma segura; si corresponde a la generación one-shot
vigente, repone su presupuesto. Ambas decisiones se registran en el audit ledger.
Los bearers productores no autorizan ninguna ruta operador.

## 8. Backup y restore

`python -m slack_correlation.store backup` usa backup online consistente de
SQLite, valida integridad/schema y publica atómicamente con permisos `0600`.
`restore` exige destino offline mediante el mismo instance lock, valida antes de
reemplazar y usa copia temporal, `fsync` y rename atómico. La validación exige las
tablas, columnas, claves, índices únicos, checks, foreign keys, singleton de
activación e invariantes lógicos exactos de V2; `user_version=2` por sí solo no es
suficiente. Véase el runbook operativo de backup/restore.

## 9. Fuera de V1

Interactivity, botones, modales, Events API, Incoming Webhooks y Socket Mode
permanecen apagados. La resolución interactiva exige firma Slack, replay durable,
allowlist de workspace/canal/usuario y revalidación contra Supabase Cloud; no se
simula dentro del contrato de avisos outbound.
