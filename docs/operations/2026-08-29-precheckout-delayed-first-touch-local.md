# Verificación local del first-touch diferido precheckout — 2026-08-29

- **Estado:** evidencia local completa; no es merge, deploy, migración Cloud, activación ni entrega real
- **Base:** `9373147dad9ac9f12fc4be075bcd34b803fa0fef`
- **Branch:** `docs/precheckout-delayed-first-touch-contract`
- **Capacidad:** `PRECHECKOUT_DELAYED_FIRST_TOUCH_ENABLED=false`

## Alcance verificado

```text
lead.precheckout V1.1.0 autorizado
→ timer durable de 60 minutos
→ revalidación fail-closed
→ presupuesto físico compartido
→ worker y sender existentes
→ máximo un POST
→ accepted_by_chatwoot o delivery_unknown
→ cero follow-ups
```

La verificación cubrió replay exacto, actualización del timer por una submission
autorizada posterior, rollback atómico, presupuesto compartido con abandono/pago
fallido, compra y señal Hotmart tardía, opt-out y otros stops, selección de la
última submission autorizada, fence transaccional inmediatamente anterior al
sender, recuperación de commands `reserved`/`request_started`, resultados ambiguos
sin retry y shutdown adversarial.

## Evidencia ejecutable

### Python

- Suite focal de migraciones, worker y configuración: exit `0`.
- Suite completa `uv run pytest -q`: exit `0`.
- Único warning: deprecación preexistente de `Starlette TestClient`.
- El proceso `spawn` del sender fue probado tanto para aceptación como para un
  sender que suprime `CancelledError`.
- Cancelaciones repetidas durante la finalización no retornan antes de la
  persistencia confirmada y preservan IDs remotos cuando existen.
- Una falla de proyección conserva la command `reserved`; el siguiente poll
  reintenta sólo la autoridad y produce un único sender call.
- Un `request_started` recuperado se terminaliza `delivery_unknown` sin sender.
- La fábrica productiva construye el sender efímero con un fence derivado del
  `target_phone` durable, no de `ALLOWED_WHATSAPP_JID`.

### SQL embebido y ACL

Los tracers focales terminaron con:

```text
PRECHECKOUT_DELAYED_TIMER_SCHEDULE_REPLAY_INERT_OK
PRECHECKOUT_DELAYED_TIMER_DEFAULT_OFF_OK
PRECHECKOUT_DELAYED_TIMER_ATOMIC_ROLLBACK_OK
PRECHECKOUT_DELAYED_RESERVATION_COMMAND_REPLAY_OK
PRECHECKOUT_DELAYED_RESERVATION_SHARED_BUDGET_OK
PRECHECKOUT_DELAYED_RESERVATION_PURCHASE_PROVIDER_OK
PRECHECKOUT_DELAYED_RESERVATION_STOPS_ZERO_EFFECT_OK
PRECHECKOUT_DELAYED_WORKER_SENDER_SQL_OK
acl_hardening=OK positive_control_leaks=6 public_functions=125 service_entrypoints=53
```

`npm test` sobre el paquete SQL completo terminó con exit `0`.

El tracer del timer crea una segunda submission autorizada para la misma intención
y comprueba que el único timer cambia su fuente y mueve `due_at` desde `16:00` a
`16:30`; una submission más vieja no puede adelantarlo. El tracer worker/sender
comprueba que commands `reserved` y `request_started` reaparecen en el due-list y
que un request-start recuperado termina sin POST.

### PostgreSQL 17 real descartable

Se creó un clúster PostgreSQL `17.10` local y temporal, se aplicó baseline más el
stack cronológico actual y se verificaron los tres entrypoints de esta macro. El
probe final terminó con:

```text
PRECHECKOUT_TASK5_REAL_POSTGRES_OK
PRECHECKOUT_TASK5_OPT_OUT_FENCE_CONCURRENCY_OK
```

Comprobó además:

- timer y reevaluación presentes;
- proyección pre-send presente;
- `PUBLIC` sin `EXECUTE` sobre la proyección;
- `service_role` con `EXECUTE`;
- `send_authorized` expuesto como gate explícito.
- carrera de dos sesiones reales: un writer de opt-out inicia y retiene su
  transacción; la autorización pre-send bloquea hasta su commit, relee el stop y
  retorna `delivery_unknown|blocked_contact` sin POST.

El postflight histórico de
`20260829000100_johanna_operator_resolution_one_shot.sql` se omitió durante el
apply, conservando su DDL, por un defecto previo y fuera de alcance ya registrado.
No se modificó esa migración ni se usó esta excepción como evidencia de su
postflight. Las migraciones `20260829000200`–`00400` sí se aplicaron completas con
sus postflights actuales.

El clúster, puerto y script temporal fueron eliminados después del probe.

## Revisión adversarial y remediación

La primera revisión integral independiente devolvió `REQUEST_CHANGES` por tres
blockers HIGH: timer anclado a una submission vieja, falta de fence atómico
pre-POST y commands no recuperables ante falla/crash. La remediación introdujo:

1. refresh monotónico del timer por la submission autorizada más reciente;
2. estado durable `reserved` separado de `request_started`;
3. RPC pre-send con locks compartidos y transición atómica;
4. due-list de recuperación para `reserved` y `request_started`;
5. recuperación de inflight como `delivery_unknown`, sin resend.

La misma revisión detectó además outcomes SQL que el parser Python no aceptaba y
una fábrica precheckout todavía fenced al JID global. Se amplió el enum exacto y la
fábrica ahora deriva el fence del teléfono durable de cada command.

### HTTP TCP y lifespan

Un servidor Uvicorn real sobre loopback arrancó el ASGI lifespan. `GET /health`
respondió `200`; el worker consultó el due-list con
`include_precheckout=true`; el fake de autoridad devolvió backlog vacío y se
comprobó delta cero de admisiones y efectos. El servidor cerró limpiamente.

Esta prueba demuestra transporte TCP local, lifespan y polling del worker. No
simula aceptación Chatwoot ni entrega WABA; esas evidencias requieren autorización
y una etapa productiva separadas.

### Calidad y coordinación

- `git diff --check`: exit `0`.
- `agent_workspace.py preflight`: exit `0`.
- Base, HEAD y merge-base coincidieron.
- Scan estático: cero shell injection, SQL formateado o secretos productivos.
  Los matches literales restantes pertenecen a credenciales ficticias de tests,
  llamadas `client.exec` del validador PGlite y un round-trip `pickle` de un objeto
  local construido por el test; no consumen entrada no confiable.

## Límites y efectos

No se ejecutó ni autorizó:

- commit, push, PR o merge;
- migración en Supabase Cloud;
- deploy o cambio en EasyPanel;
- publicación o aprobación del template Meta;
- publicación del scope o activación de flags;
- POST a Chatwoot/WABA;
- mensaje físico o E2E real.

Efectos productivos observados durante esta tarea: `0` mensajes y `0` delivery
attempts reales.
