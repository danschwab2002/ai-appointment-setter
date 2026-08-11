# Runbook — E2E controlado Hotmart → WABA para Lancemos

- **Estado:** Preparado; control plane observado, corridas inbound/outbound no ejecutadas
- **Fecha:** 2026-08-11
- **Alcance:** una oferta, un destinatario allowlisted y un primer contacto oficial
- **Prerequisito:** `docs/design/lancemos-waba-hotmart-readiness.md`
- **No es evidencia:** este documento define el procedimiento; la evidencia se crea después de una ejecución real

## 1. Reglas de seguridad

- No escribir tokens, Hottok, teléfonos, payloads completos ni IDs externos en
  Git, chat o logs.
- Los secretos viven únicamente en EasyPanel.
- Empezar con todos los efectos apagados.
- Mantener un único destinatario de prueba allowlisted.
- Presupuesto: un primer contacto; cualquier follow-up se habilita como una
  prueba separada.
- No usar Evolution ni texto libre como fallback si el scope declara `waba`.
- Detenerse en el primer gate fallido; no reparar ni cambiar configuración durante
  una corrida declarada como observación.

## 2. Registro previo de decisiones

Antes de tocar configuración debe existir un registro aprobado con:

| Dato | Estado mínimo | Evidencia permitida |
|---|---|---|
| Número/cuenta WABA | confirmado | referencia opaca o enlace al control plane |
| Account/inbox Chatwoot | confirmado | IDs sanitizados o hash de configuración |
| Producto Hotmart | confirmado | ID canónico |
| Oferta Hotmart | confirmada | offer code canónico |
| Template apertura | Juan + Meta aprobados | nombre, idioma, categoría y versión |
| Template follow-up | Juan + Meta aprobados | nombre, idioma, categoría y versión |
| Destinatario E2E | autorizado | sólo confirmación `configured=true` |
| Operador/rollback | disponible | rol o equipo, sin dato personal innecesario |

Si un valor está pendiente, el resultado es `status: blocked` con
`blocked_reason: business_input` y no se continúa.

## 3. Preflight sin efectos

### 3.1 Configuración desplegada

Con los valores ya cargados en EasyPanel, comprobar sólo presencia y consistencia;
no imprimir valores:

- variables de Chatwoot, WABA, Hotmart, Supabase y Hermes presentes;
- `LANCEMOS_PILOT_CHANNEL_PROVIDER=waba`;
- ambos nombres de template, idioma y categoría no vacíos;
- `LANCEMOS_PILOT_BOUNDARY_ENABLED=false`;
- `DURABLE_DISPATCHER_ENABLED=false`;
- `DURABLE_OUTBOUND_ENABLED=false`;
- `CHATWOOT_AUTOMATED_REPLIES_ENABLED=false`;
- `CHATWOOT_REPLY_SPLITTER_ENABLED=false`;
- `HERMES_SHADOW_ENABLED=false`;
- `RESOLUTION_WORKER_ENABLED=false` y `HOTMART_PURCHASE_WORKER_ENABLED=false`;
- `CHATWOOT_DURABLE_OPT_OUT_ENABLED=false`;
- `CHATWOOT_OPT_OUT_MACRO_ID` y
  `CHATWOOT_OPT_OUT_PROJECTION_WORKER_ID` sin configurar, porque la presencia de
  ambos puede iniciar el worker aunque el detector durable esté apagado;
- `HUMAN_HANDOFF_ADMISSION_ENABLED=false` y
  `HUMAN_HANDOFF_PROJECTION_ENABLED=false`.

Además de los flags, demostrar conteo cero de efectos de opt-out y handoff
pendientes, leased o retryable. Un worker apagado con backlog mutable no satisface
el preflight sin efectos.

Resultado esperado: el servicio arranca sin activar efectos generales.

### 3.2 Control plane de Chatwoot/WABA

Usar lecturas autenticadas y registrar sólo estados:

- account accesible;
- inbox existe, pertenece al account y corresponde a WhatsApp oficial;
- número/canal conectado;
- AgentBot/control credential poseen sólo las capacidades necesarias;
- ambos templates aparecen aprobados con el esquema esperado;
- no existen dos inboxes o templates ambiguos para la misma referencia.

Cualquier `401/403`, recurso ausente, provider incorrecto o ambigüedad bloquea la
corrida. No sustituir credenciales con un token personal más amplio.

### 3.3 Estado durable

Publicar o seleccionar el scope con automatización `inactive`, nunca armado por
el acto de publicación. Verificar:

- tenant, account/inbox, provider/cuenta, producto y oferta exactos;
- policy key/version fijados;
- cohorte limitada al destinatario autorizado;
- presupuesto de un request;
- kill switch conocido y operable.

`GET /ready` debe responder sin PII y declarar un estado coherente con runtime
inactivo. Un `503` o mismatch conserva `no-go`.

## 4. Corrida inbound previa al pago, sin respuesta

Esta corrida es independiente del primer contacto iniciado por template. Sólo
demuestra la ruta real `WABA → Chatwoot → webhook del bridge` y no requiere método
de pago ni templates aprobados.

### 4.1 Gates específicos

Antes de pedir el mensaje al teléfono de prueba, exigir:

```text
CHATWOOT_ACCOUNT_ID=<account verificado>
CHATWOOT_INBOX_ID=<inbox WABA verificado>
MESSAGING_CHANNEL=waba
CHATWOOT_AUTOMATED_REPLIES_ENABLED=false
CHATWOOT_REPLY_SPLITTER_ENABLED=false
HERMES_SHADOW_ENABLED=false
CHATWOOT_DURABLE_OPT_OUT_ENABLED=false
CHATWOOT_OPT_OUT_MACRO_ID=<unset>
CHATWOOT_OPT_OUT_PROJECTION_WORKER_ID=<unset>
HUMAN_HANDOFF_ADMISSION_ENABLED=false
HUMAN_HANDOFF_PROJECTION_ENABLED=false
LANCEMOS_PILOT_BOUNDARY_ENABLED=false
RESOLUTION_WORKER_ENABLED=false
HOTMART_PURCHASE_WORKER_ENABLED=false
DURABLE_DISPATCHER_ENABLED=false
DURABLE_OUTBOUND_ENABLED=false
```

Además:

- la revisión desplegada debe validar `account.id`, `inbox.id` y
  `conversation.inbox_id` antes de persistir el webhook;
- Evolution debe permanecer desconectado y su integración Chatwoot deshabilitada,
  sin borrar el inbox histórico;
- el webhook compartido debe seguir suscrito sólo a `message_created`;
- no debe existir trabajo outbound vivo ni un request iniciado sin reconciliar;
- los backlogs de proyección de opt-out y handoff deben ser exactamente cero;
- `CAPTURE_DIR/.work` no debe contener una admisión previa procesable;
- el único JID de prueba debe estar configurado sólo en el secret store.

Generar un snapshot sanitizado y ejecutar:

```text
uv run python scripts/verify_chatwoot_waba_readiness.py \
  --expected-account-id "$ACCOUNT_ID" \
  --expected-waba-inbox-id "$WABA_INBOX_ID" \
  --expected-legacy-inbox-id "$LEGACY_INBOX_ID" < snapshot.json
```

Sólo `status=ready`, `safe_for_controlled_inbound=true` y exit code `0` permiten
continuar. El 2026-08-11 el probe real produjo `blocked`: el bridge desplegado
seguía apuntando a Evolution con efectos habilitados, la integración Evolution →
Chatwoot seguía activa y no existía Team humano.

El snapshot que alimenta el verificador debe declarar explícitamente todos los
flags anteriores y los dos resultados `*_projection_backlog_zero=true`. Campos
ausentes, `null`, strings o enteros usados como booleanos bloquean la corrida; el
verificador no infiere defaults del runtime.

### 4.2 Única acción manual

Con operador presente, el teléfono allowlisted envía **un** mensaje entrante al
número oficial. No se responde desde Chatwoot, no se cambia el estado de la
conversación y no se habilita ningún worker durante la observación.

### 4.3 Evidencia obligatoria

Registrar por separado, sin contenido ni IDs externos:

1. dispositivo confirmó que envió un único mensaje al número oficial;
2. Chatwoot creó o reutilizó una conversación en el inbox WABA esperado;
3. Chatwoot emitió un webhook `message_created` autenticado;
4. el bridge respondió `202 captured` una vez;
5. replay del mismo delivery produjo `200 duplicate` y ninguna segunda captura;
6. mismo JID con account/inbox incorrecto produjo `200 ignored` con
   `inbox_not_allowed` o `account_not_allowed`;
7. cero llamadas a Hermes, cero replies, cero request-start y cero mensajes al
   dispositivo desde el negocio.

Un `2xx` de Chatwoot, un payload manual o ver la conversación en UI no sustituye
las demás capas. Si aparece cualquier respuesta externa, detener la observación y
considerar la corrida `fail`; todos los flags permanecen apagados durante el
diagnóstico.

### 4.4 Rollback de esta corrida

La corrida inbound no activa nada y su rollback normal es conservar todos los
flags de 4.1 en `false`/`unset`. Si aparece un efecto inesperado:

1. detener el servicio bridge antes de investigar;
2. no borrar la conversación, capturas ni efectos durables;
3. confirmar nuevamente que los workers de reply, shadow, resolución, compra,
   dispatcher, opt-out y handoff no fueron construidos;
4. verificar backlog de ambas proyecciones y reconciliar cualquier lease o efecto
   incierto sin retry ciego;
5. reanudar sólo con un snapshot nuevo que vuelva a producir `status=ready`.

## 5. Corrida controlada de primer contacto

### 5.1 Barrera de backlog cero

Antes de poner el runtime en `armed`, el origen Hotmart debe permanecer
desconectado o temporalmente suspendido y nadie debe emitir eventos manuales. Una
lectura autoritativa debe demostrar simultáneamente:

- cero eventos Hotmart pendientes o en procesamiento que puedan ser tomados por
  `ResolutionWorker`;
- cero casos, secuencias y acciones vivas para el contacto de prueba y para la
  versión del scope;
- cero intentos `reserved` o `request_started`, autorizaciones outbound y
  resultados `delivery_unknown` para ese alcance;
- cohorte vacía salvo por el único contacto de prueba;
- presupuesto consumido igual a cero.

La consulta debe ejecutarse nuevamente justo antes de `armed`. Los conteos se
registran sin IDs ni PII. Cualquier valor distinto de cero bloquea la corrida; no
se reutiliza ni se borra trabajo previo para hacerla pasar.

Después, arrancar una fase de observación con esta configuración exacta:

```text
LANCEMOS_PILOT_BOUNDARY_ENABLED=true
RESOLUTION_WORKER_ENABLED=false
HOTMART_PURCHASE_WORKER_ENABLED=false
DURABLE_DISPATCHER_ENABLED=true
DURABLE_OUTBOUND_ENABLED=false
```

El control durable permanece `inactive`. Esperar al menos dos ciclos del
dispatcher y volver a comprobar backlog, autorizaciones y contadores remotos:
deben seguir en cero. `/ready` debe informar `pilot_runtime_inactive`. Esta fase
prueba que el proceso y el dispatcher arrancan sin sender, pero no prueba WABA.

Sólo si ambas lecturas son cero puede prepararse la activación. El ingreso sigue
quiescente hasta que `/ready` confirme `pilot_runtime_armed`.

### 5.2 Fuente del evento

Elegir y registrar una sola modalidad:

- `hotmart_real`: Hotmart emite el webhook desde la cuenta configurada; o
- `manual_official_v2`: se reproduce por HTTPS un payload oficial V2 con Hottok
  válido y nuevos IDs de prueba.

La modalidad manual no se registra como prueba de entrega real de Hotmart.

### 5.3 Activación acotada

Sólo después del preflight:

1. confirmar por tercera vez que el origen sigue quiescente y el backlog es cero;
2. enrolar únicamente el contacto de prueba y fijar presupuesto de un request;
3. iniciar `ResolutionWorker` y `DurableDispatcher` con el perímetro habilitado,
   pero mantener `DURABLE_OUTBOUND_ENABLED=false` y el runtime `inactive`;
4. mediante `set_lancemos_pilot_runtime_state`, cambiar de `inactive` a `armed`
   usando la generación esperada y registrar la nueva generación;
5. habilitar `DURABLE_OUTBOUND_ENABLED=true`, reiniciar el proceso y exigir que
   `/ready` informe `pilot_runtime_armed` antes de abrir el ingreso;
6. habilitar exclusivamente la modalidad de fuente elegida y emitir un único
   evento nuevo para el producto/oferta aprobados;
7. cerrar nuevamente el ingreso y no emitir otro evento hasta observar el estado
   terminal del primero.

La configuración de la fase activa es:

```text
LANCEMOS_PILOT_BOUNDARY_ENABLED=true
RESOLUTION_WORKER_ENABLED=true
HOTMART_PURCHASE_WORKER_ENABLED=true
DURABLE_DISPATCHER_ENABLED=true
DURABLE_OUTBOUND_ENABLED=true
```

No se apaga `LANCEMOS_PILOT_BOUNDARY_ENABLED` mientras cualquiera de esos
workers permanezca habilitado: el factory rechaza esa combinación.

### 5.4 Postcondiciones obligatorias

Registrar sin PII:

- webhook autenticado y admitido una vez;
- caso, secuencia, acción e intento vinculados al scope esperado;
- `approved_template` como modo durable;
- payload remoto con `template_params` correcto;
- una sola conversación canónica en el inbox esperado;
- una sola llegada física al WhatsApp de prueba;
- message/conversation IDs remotos reconciliados mediante hashes o referencias
  internas, no valores publicados;
- presupuesto consumido exactamente una vez;
- cero uso de Evolution o freeform.

El usuario de prueba confirma la llegada física; un `2xx` de Chatwoot no basta.

## 6. Probes negativos y replay

Con outbound todavía acotado:

1. repetir exactamente el mismo evento: cero mensaje adicional;
2. repetir la semántica con delivery ID distinto: cero mensaje adicional;
3. evento con producto incorrecto: rechazo antes del modelo y del sender;
4. evento con oferta incorrecta: rechazo antes del modelo y del sender;
5. destinatario no allowlisted: cero lookup/create/send de Chatwoot;
6. combinación WABA + freeform: rechazo en request-start;
7. respuesta ambigua del provider: `delivery_unknown`, sin retry ciego.

Cada probe debe demostrar progreso positivo en el camino válido y contadores cero
en el camino bloqueado; un worker muerto no cuenta como prueba de seguridad.

## 7. Follow-up separado

No se incluye automáticamente en la primera corrida. Para probarlo:

- mantener la misma conversación canónica;
- usar el template de follow-up aprobado por Juan y Meta;
- comprimir la política sólo en una versión de prueba explícita;
- verificar que no se crea otro contacto ni otra conversación;
- insertar antes del request-start pruebas de compra, opt-out y takeover, cada una
  en una corrida independiente;
- exigir cero mensaje cuando cualquiera de esos stops gana.

## 8. Rollback

Después de la corrida, o ante el primer fallo:

1. cerrar primero el ingreso Hotmart/manual para que no entren eventos nuevos;
2. mediante `set_lancemos_pilot_runtime_state`, cambiar `armed → paused` con la
   generación esperada; `/ready` debe informar `pilot_runtime_paused`;
3. mantener `DURABLE_DISPATCHER_ENABLED=true` y
   `DURABLE_OUTBOUND_ENABLED=true` sólo mientras se finaliza o reconcilia un
   request que ya cruzó `request_started`; la pausa impide nuevos request-start;
4. confirmar backlog cero o `delivery_unknown` explícitamente retenido, sin
   sucesor ni retry ciego;
5. cambiar `DURABLE_OUTBOUND_ENABLED=false`,
   `HOTMART_PURCHASE_WORKER_ENABLED=false` y
   `RESOLUTION_WORKER_ENABLED=false`, y reiniciar;
6. conservar `LANCEMOS_PILOT_BOUNDARY_ENABLED=true` con runtime `paused`; sólo un
   apagado total puede deshabilitar también dispatcher y perímetro, en ese orden;
7. mantener la cohorte general vacía y verificar `/ready` sanitizado;
8. no borrar evidencia durable para “limpiar” la prueba.

## 9. Evidencia de salida

Crear un documento fechado nuevo en `docs/operations/` con:

```yaml
status: pass | fail | blocked
blocked_reason: external | deployment | implementation | business_input | null
source_mode: hotmart_real | manual_official_v2
waba_control_plane_verified: true | false
juan_templates_approved: true | false
meta_templates_approved: true | false
physical_arrival_confirmed: true | false
first_touch_count: 0 | 1
replay_additional_count: 0
wrong_scope_send_count: 0
outbound_mode: approved_template
rollback_verified: true | false
limitations: []
```

No incluir contenido de mensajes, teléfonos, emails, tokens, payloads completos ni
IDs externos sin sanitizar.

## 10. Veredicto

- `pass`: todas las postcondiciones, probes y rollback fueron observados.
- `fail`: se ejecutó la frontera y una postcondición fue incorrecta.
- `blocked`: faltó una decisión, permiso, recurso o aprobación externa y no hubo
  efecto.

Un `pass` habilita recién la evaluación integral de go/no-go; no activa una
cohorte real por sí solo.
