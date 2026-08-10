# Runbook — E2E controlado Hotmart → WABA para Lancemos

- **Estado:** Preparado; no ejecutado
- **Fecha:** 2026-08-10
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

Si un valor está pendiente, el resultado es `blocked_missing_business_input` y no
se continúa.

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
- `CHATWOOT_AUTOMATED_REPLIES_ENABLED=false` salvo que exista una prueba inbound
  separada y autorizada.

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

## 4. Corrida controlada de primer contacto

### 4.1 Barrera de backlog cero

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

### 4.2 Fuente del evento

Elegir y registrar una sola modalidad:

- `hotmart_real`: Hotmart emite el webhook desde la cuenta configurada; o
- `manual_official_v2`: se reproduce por HTTPS un payload oficial V2 con Hottok
  válido y nuevos IDs de prueba.

La modalidad manual no se registra como prueba de entrega real de Hotmart.

### 4.3 Activación acotada

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

### 4.4 Postcondiciones obligatorias

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

## 5. Probes negativos y replay

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

## 6. Follow-up separado

No se incluye automáticamente en la primera corrida. Para probarlo:

- mantener la misma conversación canónica;
- usar el template de follow-up aprobado por Juan y Meta;
- comprimir la política sólo en una versión de prueba explícita;
- verificar que no se crea otro contacto ni otra conversación;
- insertar antes del request-start pruebas de compra, opt-out y takeover, cada una
  en una corrida independiente;
- exigir cero mensaje cuando cualquiera de esos stops gana.

## 7. Rollback

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

## 8. Evidencia de salida

Crear un documento fechado nuevo en `docs/operations/` con:

```yaml
status: pass | fail | blocked
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

## 9. Veredicto

- `pass`: todas las postcondiciones, probes y rollback fueron observados.
- `fail`: se ejecutó la frontera y una postcondición fue incorrecta.
- `blocked`: faltó una decisión, permiso, recurso o aprobación externa y no hubo
  efecto.

Un `pass` habilita recién la evaluación integral de go/no-go; no activa una
cohorte real por sí solo.
