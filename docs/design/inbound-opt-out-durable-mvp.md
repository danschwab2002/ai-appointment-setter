# Opt-out inbound durable para el MVP de Lancemos

- **Estado:** Implementada localmente; pendiente de integración y despliegue
- **Fecha:** 2026-08-09
- **Revisión adversarial:** PASS durable-workflow y PASS producto/bridge
- **Prioridad de producto:** [Próxima prioridad del MVP](./lancemos-mvp-next-priority.md)
- **Alcance:** WhatsApp, propósito `cart_recovery`, una oferta y un piloto supervisado
- **No implica:** DDL aplicado en producción, worker productivo ni habilitación de outbound

## 1. Objetivo

Una solicitud inequívoca de no recibir más mensajes debe convertirse en una
restricción durable antes de invocar Hermes y debe ganar frente a cualquier
permiso, seguimiento, retry o cambio posterior de política.

```text
mensaje público entrante admitido
→ lote canónico después del debounce
→ detección determinística de opt-out
→ correlación estricta con contacto/conversación internos
→ transición SQL atómica y auditable
→ cancelación o preservación segura de efectos
→ señal operativa en Chatwoot
→ sin invocación a Hermes
```

La ausencia de coincidencia no significa consentimiento: sólo significa que este
control no aplicó una baja automática. Las demás autorizaciones y guardas siguen
siendo obligatorias.

## 2. Decisiones propuestas

### 2.1 Detectar sobre el lote canónico, no sobre el webhook aislado

El worker debe evaluar todos los mensajes públicos entrantes que pertenecen al
turno agrupado de 30 segundos, después de comprobar que cada `message_id` del
batch existe en el historial canónico acotado de Chatwoot.

Esto evita dos errores:

- perder una baja porque el último mensaje del lote sólo dice «gracias»;
- usar contenido crudo o fuera de orden que Chatwoot todavía no confirmó.

La detección ocurre después de autenticación, anti-replay, allowlist, admisión
durable, debounce y validación de historial; ocurre antes del shadow processor,
Hermes, reply splitting o cualquier respuesta automática.

### 2.2 Empezar con un detector estricto de frases globales

El MVP sólo aplicará automáticamente frases cuyo significado sea global e
inequívoco, por ejemplo familias equivalentes a:

- «no me escriban más»;
- «no más mensajes», como mensaje completo;
- «dejen de contactarme»;
- «quiero darme de baja»;
- «no quiero recibir más mensajes».

La implementación utilizará normalización Unicode, minúsculas, espacios y
puntuación, seguida por patrones cerrados y testeados. No utilizará substring
libre, similitud semántica ni un modelo como autoridad.

El literal breve «No más mensajes», indicado por Marcela para la operación de
Lancemos, utiliza la misma transición global `stop_receiving_messages`. Sólo
coincide como mensaje completo después de normalizar puntuación y mayúsculas;
una cita o una referencia dentro de otra frase no activa la baja.

Quedan fuera de la baja automática:

- negaciones: «no quiero dejar de recibir mensajes»;
- referencias a terceros o citas;
- pedidos sobre otra cosa: «dame de baja el precio»;
- frases ambiguas: «ahora no», «más tarde», «no gracias»;
- multimedia sin texto soportado;
- interpretación de intención por contexto generativo.

Los casos ambiguos continúan por el flujo normal o se escalan según la release
conversacional; nunca se convierten silenciosamente en una baja durable.

### 2.3 Tratar una frase global como opt-out global del contacto para WhatsApp

Para las frases aceptadas, la transición recomendada es:

- `contacts.contact_permission = 'opted_out'`;
- `contacts.lifecycle_status = 'do_not_contact'`;
- nueva autorización `denied` para `whatsapp/cart_recovery`, con fuente CRM;
- cierre temporal de autorizaciones `allowed` vigentes del mismo canal/propósito;
- evidencia mínima del mensaje canónico, sin copiar su texto.

Aunque el piloto sólo use `cart_recovery`, actualizar también el estado global
evita que una futura finalidad comercial interprete la frase «no me escriban
más» como una baja limitada al carrito. Una reautorización futura debe ser una
operación manual, explícita y auditada; nunca un nuevo abandono automático.

### 2.4 No enviar confirmación automática en la primera versión

El default seguro es silencio externo después de aplicar la baja. La transición
puede agregar una etiqueta o señal interna en Chatwoot, pero no genera otra
respuesta comercial ni una nueva secuencia.

Una confirmación única sólo se incorporará si Juan aprueba el copy, la modalidad
de canal y su base operativa. Debe diseñarse como efecto separado e idempotente,
no como excepción informal a la restricción recién aplicada.

## 3. Frontera determinística

Hermes no participa en:

- reconocer una baja automática;
- resolver el contacto o conversación canónicos;
- decidir la precedencia de autorizaciones;
- cancelar acciones;
- reautorizar un contacto;
- confirmar que un efecto externo ocurrió.

El modelo puede responder a frases ambiguas únicamente después de que el bridge
haya determinado que no existe una baja inequívoca y que el resto de las guardas
permite invocarlo.

## 4. Contrato durable propuesto

### 4.1 Evidencia idempotente

Agregar una evidencia durable con identidad única equivalente a:

```text
ContactOptOutEvent
  id
  contact_id?
  channel = whatsapp
  purpose = cart_recovery
  source = chatwoot
  canonical_account_id
  canonical_inbox_id
  canonical_conversation_id
  canonical_message_id
  occurred_at
  normalized_rule_key
  correlation_status
  projection_status
  projection_attempt_count
  projection_next_attempt_at?
  projection_error_code?
  created_at
```

La combinación fuente + cuenta + inbox + conversación canónica + mensaje
canónico debe impedir aplicar dos veces el mismo evento. Un replay sólo devuelve
`already_applied` si también coinciden `contact_id`, canal, propósito,
`occurred_at`, `normalized_rule_key`, conversación y mensaje. Cualquier diferencia
devuelve `evidence_conflict` y no modifica estado. No se persiste el texto
completo en esta evidencia.

La evidencia se admite aun cuando todavía no exista una correlación interna
inequívoca. En ese caso conserva la identidad canónica no-PII de Chatwoot
(`account_id`, `inbox_id`, `conversation_id`, `message_id`) con estado
`unmatched` o `ambiguous`. Ese stop fact bloquea Hermes, respuestas y futuros
turnos de la misma conversación hasta ser conciliado por un proceso durable.

### 4.2 RPC de transición

Una primera RPC service-role-only admite la señal canónica y su resultado de
correlación. Si la correlación es cero o ambigua, persiste el stop fact y termina
con `recorded_unmatched | recorded_ambiguous`; nunca permite continuar hacia
Hermes. Un reconciliador reintenta la resolución y sólo una coincidencia exacta
puede promoverla a `applied`.

La RPC de aplicación recibe identificadores internos y evidencia canónica
estrictamente validada. Dentro de una transacción:

1. bloquea el contacto;
2. detecta replay por evidencia;
3. persiste el evento de opt-out;
4. actualiza el estado global del contacto;
5. vence permisos `allowed` vigentes del canal/propósito;
6. inserta o reutiliza una autorización `denied` sin vencimiento;
7. bloquea casos vivos del contacto para `cart_recovery`;
8. completa/cancela secuencias activas;
9. terminaliza de forma exacta acciones/intentos que no tienen
   `request_started` durable;
10. convierte todo intento `request_started` todavía no resuelto en
    `completed/delivery_unknown`, con deadline de reconciliación futuro;
11. impide crear sucesoras;
12. escribe auditoría en la misma transacción;
13. devuelve `applied | already_applied | evidence_conflict` y conteos internos.

La admisión del stop fact acepta sólo IDs canónicos de Chatwoot ya autenticados y
validados; éstos bastan para bloquear esa conversación, pero no son autoridad
para mutar un contacto. La transición comercial sólo recibe IDs internos después
de una correlación inequívoca. Ninguna RPC acepta el texto libre ni un JID como
prueba directa de identidad interna.

### 4.3 Locks: inventario obligatorio antes del DDL

El motor actual no usa un único orden universal: la planificación bloquea el
evento antes del contacto, mientras reserva/finalización parten del caso y no
siempre bloquean el contacto. Por eso el DDL no puede asumir como existente el
orden `contacto → caso`.

Antes de aprobar la migración se debe inventariar toda función que escriba
eventos, contactos, channel identities, autorizaciones, casos, secuencias,
acciones o intentos. El nuevo orden debe ser compatible con esas rutas e incluir
explícitamente evidencia y channel identity. Todos los conjuntos se bloquean en
orden UUID estable. La prueba PostgreSQL debe intercalar opt-out con
planificación, reserva, request-start, finalización y reconciliación, usando
timeouts para demostrar ausencia de deadlock.

La invariantes funcionales son independientes del orden elegido: una denegación
vigente se evalúa antes de `allowed`, y ninguna planificación concurrente puede
pisarla.

## 4.4 Privilegios y protección física

Decir «RPC-only» no alcanza mientras `service_role` conserve DML directo. La
implementación debe inventariar los grants efectivos y cerrar todos los bypasses
de las filas/campos protegidos:

- la RPC nueva y los helpers internos no son ejecutables por `PUBLIC`, `anon` ni
  `authenticated`;
- sólo el entrypoint necesario es ejecutable por `service_role`;
- los helpers internos se revocan también a `service_role`;
- evidencias y denegaciones son inmutables salvo una conciliación declarada;
- los estados terminales y la identidad de intentos no pueden reescribirse por
  DML directo;
- si funciones existentes necesitan DML, se migran a una frontera
  `SECURITY DEFINER` con owner no-login y `search_path` fijo, o se instalan
  triggers físicos equivalentes antes de revocar grants;
- el postflight usa `has_function_privilege`, `has_table_privilege` e intentos
  reales de `INSERT/UPDATE/DELETE` bajo cada rol.

No se acepta una migración que sólo revoque funciones pero deje posible borrar
un denial, cambiar `contacts.contact_permission` o reescribir el outcome de un
attempt desde PostgREST/service-role.

## 5. Semántica de cancelación por fase

### Antes de `request_started`

- `pending`, `deferred` y `retryable_failed`: pasan a `cancelled`;
- un intento `reserved` pasa exactamente a `phase=completed`,
  `outcome=failed_before_request`, `reason_code=contact_opted_out`, sin remote
  message, accepted message, retry ni reconciliation deadline;
- la acción pasa a `cancelled`, conserva identidad/idempotency key, limpia lease
  y registra `terminal_reason=contact_opted_out`;
- el replay compara attempt, action, lease generation y todos los campos
  terminales; una diferencia es conflicto, no éxito idempotente;
- esta transición pertenece a la RPC dedicada de opt-out. No reutiliza la
  finalización genérica que convertiría el fallo en retryable/permanent.

### Después de `request_started`

El commit de `request_started` es la frontera executable actual: puede ocurrir
antes del POST real. Por eso el sistema no puede afirmar que el mensaje no salió
ni prometer que el POST no comenzará después de un opt-out concurrente. Debe:

- convertir atómicamente todo intento no resuelto a `phase=completed`,
  `outcome=delivery_unknown`, `reason_code=contact_opted_out_after_request_started`
  y un `reconciliation_deadline` futuro y acotado;
- dejar la acción en `delivery_unknown`, sin lease, pero cerrar el caso/secuencia
  por opt-out para que no exista sucesor;
- conservar el contacto/caso bloqueado;
- permitir que `record_and_finalize_followup_acceptance` reciba evidencia
  aceptada tardía dentro del deadline sin reabrir caso/secuencia ni crear
  sucesor;
- extender la reconciliación para que `not_applied` terminalice la acción como
  `cancelled` sin retry cuando el stop autoritativo sigue vigente;
- al vencer el deadline sin resolución, conservar evidencia unknown y escalar
  para revisión, nunca reintentar;
- nunca repetir el POST a ciegas.

La garantía ejecutable queda formulada así: **ningún intento que carezca de un
`request_started` durable puede enviar**. Todo intento que ya lo tenga se trata
como potencialmente entregado aunque el transporte todavía no haya comenzado.

### Efecto ya aceptado

La baja no reescribe la historia. Cancela sólo trabajo futuro y bloquea nuevas
intenciones comerciales.

## 6. Integración con el worker Chatwoot

El corte recomendado es refactorizar la lectura canónica actual para producir un
objeto interno reutilizable:

```text
CanonicalInboundTurn
  chatwoot_account_id
  chatwoot_inbox_id
  chatwoot_conversation_id
  trigger_message_id
  batch_message_ids
  ordered_public_messages
  correlation_status
  conversation_internal_id?
  contact_internal_id?
  validated_at
```

Flujo del handler:

```text
classify_chatwoot_event
→ construir y validar CanonicalInboundTurn
→ detectar opt-out sobre mensajes del batch
  → si aplica: admitir stop fact durable; terminar siempre
    → correlación exacta: aplicar transición
    → cero/ambigua: bloquear conversación y conciliar después
  → si no aplica: continuar shadow/Hermes/reply
```

El detector no examina mensajes históricos anteriores al batch para crear una
nueva baja; esos mensajes sirven como contexto conversacional, no como el evento
actual.

La correlación usa el `account_id`, inbox configurado, conversación canónica,
mensaje canónico y sender/JID ya validados para buscar exactamente una
`conversation`/`channel_identity`/`contact` interna consistente. Cero o más de
una coincidencia son resultados durables `unmatched`/`ambiguous`; nunca caen al
camino generativo. Antes de procesar cualquier inbound posterior de esa
conversación, el worker consulta si existe un stop fact pendiente y vuelve a
bloquearlo.

## 7. Señal operativa en Chatwoot

Después del commit SQL, una proyección durable intenta aplicar un macro dedicado
de Chatwoot, configurado por `CHATWOOT_OPT_OUT_MACRO_ID`. No se reutiliza el
`CHATWOOT_PAUSE_MACRO_ID`: el contrato actual sólo ejecuta ese macro fijo y no
puede prometer una segunda etiqueta arbitraria.

El macro de opt-out debe producir una señal visible acordada (por ejemplo,
`automation_opted_out`) y pausar automatización. El bridge confirma ambas
postcondiciones. La evidencia conserva `projection_status` (`pending`,
`applied`, `retryable_failed`, `dead_letter`), contador, próxima ejecución y
error tipado. Un worker con lease reclama proyecciones pendientes; usa backoff
acotado y pasa a dead letter tras el máximo configurado. El fallback operativo
es una vista/reporte de stop facts no proyectados y una alerta privacy-safe.

La base de datos sigue siendo autoridad si Chatwoot falla. Un fallo del macro no
revierte la baja ni permite responder. Los logs sólo emiten IDs
internos/correlation hashes y reason codes estables:

```text
chatwoot_opt_out_applied
chatwoot_opt_out_duplicate
chatwoot_opt_out_crm_sync_pending
chatwoot_opt_out_correlation_failed
```

No se registra texto, teléfono, JID ni payload completo.

## 8. Feature flag y activación

Agregar un flag default-off equivalente a:

```text
CHATWOOT_DURABLE_OPT_OUT_ENABLED=false
```

Con el flag apagado, el comportamiento ejecutable actual no cambia. El flag sólo
se habilita después de:

- migración y privilegios verificados;
- detector y RPC probados;
- bridge desplegado con configuración completa;
- E2E controlado con JID allowlisted;
- ejemplos y política aprobados.

El estado durable de opt-out, una vez escrito, debe seguir siendo respetado por
todos los caminos salientes aunque el detector se apague después.

## 9. Matriz TDD y adversarial

### Detector

- cada frase positiva aprobada;
- mayúsculas, tildes, Unicode, puntuación y espacios;
- cada falso positivo conocido;
- negaciones y texto citado;
- mensaje compuesto;
- payload sin texto o tipo no soportado;
- tiempo lineal y límites de tamaño.

### Canonicalidad

- uno de varios mensajes del batch contiene la baja;
- trigger posterior dice sólo «gracias»;
- batch incompleto falla antes del detector;
- mensaje no perteneciente al batch no dispara baja;
- actor humano/bot/private/outgoing no se interpreta como opt-out;
- conversación, inbox, JID o contacto ambiguos fallan cerrados.

### SQL

- aplicación directa;
- replay exacto;
- dos sesiones aplicando el mismo mensaje;
- abandono concurrente intentando otorgar `allowed`;
- varias autorizaciones allowed históricas;
- denial existente;
- casos múltiples del mismo contacto;
- acción pendiente, leased, reserved, request_started y delivery_unknown;
- aceptación tardía después del opt-out;
- replay sin auditoría duplicada;
- grants efectivos para `anon`, `authenticated` y `service_role`.

### Worker y recuperación

- RPC aplicada implica cero llamadas a Hermes;
- RPC `already_applied` implica cero respuesta comercial;
- caída después del commit SQL y antes de la etiqueta CRM;
- retry completa la etiqueta sin reaplicar la baja;
- timeout SQL no permite responder;
- reinicio con admisión durable pendiente;
- otro inbound posterior sigue bloqueado.
- correlación cero/ambigua persiste stop fact y bloquea el turno;
- reconciliación posterior exacta aplica la baja una sola vez;
- macro de opt-out pendiente sobrevive reinicio, reintenta y dead-letterea;
- dead letter permanece visible en el reporte operativo.

### E2E controlado

1. iniciar caso con el JID permitido;
2. admitir una baja inequívoca firmada;
3. comprobar estado durable y cancelación;
4. confirmar cero invocaciones/sends posteriores;
5. repetir delivery y semantic event;
6. reiniciar el bridge;
7. intentar nuevo follow-up y nuevo abandono;
8. confirmar que denial continúa ganando;
9. verificar etiqueta/visibilidad operativa;
10. limpiar fixtures sin residuos ni PII en evidencia compartida.

## 10. Criterios de aceptación

- la baja se decide sobre un turno canónico completo;
- se persiste antes de cualquier llamada generativa;
- la transición es atómica, idempotente y auditable;
- `denied` gana frente a grants concurrentes o posteriores;
- ningún intento sin `request_started` durable puede enviar; uno que ya lo tenga
  se considera potencialmente entregado aunque el POST empiece después;
- efectos iniciados se preservan como realidad incierta/aceptada sin sucesor;
- no existe respuesta automática en el MVP inicial;
- el operador puede observar la baja o un fallo durable de proyección;
- los falsos positivos aprobados no aplican restricción;
- tests locales, SQL ejecutable, concurrencia PostgreSQL, HTTP real y E2E
  allowlisted quedan documentados por separado;
- outbound productivo continúa apagado hasta el go/no-go del piloto.

## 11. Temas abiertos

Antes de activar, Juan o el responsable del piloto debe confirmar:

- listado inicial de frases globales inequívocas;
- default de silencio externo o confirmación única;
- responsable autorizado para una corrección manual;
- SLA de casos ambiguos;
- etiqueta visible y procedimiento operativo en Chatwoot.

La recomendación actual es: detector estricto, opt-out global para WhatsApp,
silencio externo y corrección sólo manual/auditada.
