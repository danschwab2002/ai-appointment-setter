# Diseño detallado: motor de seguimientos

- **Estado:** base de diseño aprobada; formalizada por ADR-0007 y contrato V1
- **Fecha:** 2026-08-02
- **Alcance inicial:** recuperación de carritos Hotmart por WhatsApp
- **Objetivo:** definir el comportamiento del motor antes de diseñar migraciones o escribir código

## 1. Resumen

El motor se modelará como un **motor durable de próxima acción**. No será un
conjunto de cronjobs por prospecto ni un workflow genérico.

Este documento desarrolló dos diferencias entre decisiones anteriores y la
implementación E2E validada:

- ADR-0003 todavía atribuye al agente parte de la decisión de enviar o
  abstenerse, mientras el bridge actual calcula una decisión obligatoria;
- ADR-0004 de mensajería describe un único contrato template para WABA, mientras
  la ventana de servicio requiere distinguir texto libre permitido de template
  obligatorio.

ADR-0007 formaliza ambas supersesiones parciales. Este documento conserva el
razonamiento detallado que respalda esa decisión y su contrato V1.

Para cada caso comercial mantendrá:

1. una política inmutable y versionada;
2. una instancia de secuencia que registra el objetivo y su avance;
3. como máximo una próxima acción materializada;
4. una condición tipada que determina si esa acción continúa vigente;
5. un historial auditable de decisiones, intentos y resultados.

Un scheduler detectará acciones cuyo momento ya llegó. Antes de producir cualquier
efecto externo, un worker reconstruirá el contexto actual y decidirá de forma
determinística si corresponde ejecutar, diferir, reemplazar, omitir, cancelar,
pausar o escalar.

La llegada de la hora significa **volver a evaluar**, no autoriza por sí sola un
envío.

## 2. Principios

### 2.1. Fuentes de verdad

**Chatwoot es canónico para:**

- conversaciones y sus IDs externos;
- mensajes, orden y actores;
- respuesta entrante;
- human takeover;
- etiquetas y capacidad actual de responder;
- aceptación del mensaje por el gateway conversacional.

**Supabase es canónico para:**

- casos comerciales;
- políticas publicadas y versiones;
- instancias de secuencia;
- próximas acciones;
- claims, leases y reintentos;
- resultados del motor;
- auditoría operativa.

Las conversaciones o mensajes almacenados en Supabase son proyecciones y
referencias. Nunca reemplazan una consulta final a Chatwoot para autorizar un
envío.

### 2.2. Frontera del modelo

Hermes puede:

- redactar un mensaje libre cuando el canal lo permite;
- completar datos para un template aprobado;
- explicar o proponer una política desde el Automation Expert.
- clasificar o proponer una estrategia comercial cuando los hechos admiten más
  de una alternativa segura, siempre dentro de opciones permitidas por el bridge.

Hermes no puede:

- programar directamente acciones;
- reclamar tareas;
- decidir identidad o destinatario;
- autorizar un envío;
- ignorar pausas, opt-out, horarios o límites;
- modificar una política publicada;
- avanzar una secuencia por sí mismo.

La separación propuesta es:

- **guardas determinísticas:** identidad, consentimiento, opt-out, compra
  observada, takeover, estado del canal, horario, frecuencia, versiones e
  idempotencia;
- **juicio del agente:** estrategia y redacción entre alternativas que ya
  superaron las guardas;
- **decisión ejecutable:** intersección entre la propuesta válida y la acción
  autorizada por el bridge.

El agente nunca puede convertir una guarda bloqueante en permiso.

### 2.3. Garantía entre sistemas

No existe una transacción distribuida entre Hotmart, Supabase y Chatwoot. Una
compra, respuesta o intervención humana puede aparecer en la pequeña ventana
entre la última validación y el envío.

El sistema ofrece validación cercana al efecto, supresión y reconciliación
best-effort, no la garantía absoluta de que ningún evento concurrente pueda
cruzarse con un envío ya iniciado.

### 2.4. Materializar sólo la próxima acción

No se crearán por adelantado todos los pasos futuros. Después de cada resultado
se calculará únicamente el próximo paso vigente.

Esto permite absorber respuestas, compras, pausas, cambios de política y errores
sin tener que cancelar grandes cantidades de timers obsoletos.

### 2.5. Una acción programada es una intención condicionada

Toda acción declarará:

- qué se pretende hacer;
- cuándo corresponde reevaluarlo;
- hasta cuándo conserva utilidad;
- qué hecho la originó;
- qué condición debe continuar vigente;
- qué versiones del caso, secuencia y conversación espera encontrar.

## 3. Componentes conceptuales

### 3.1. Política lógica

Identidad estable administrada por la aplicación, por ejemplo:

```text
cart-recovery-standard
```

No contiene estado de ejecución.

### 3.2. Versión de política

Snapshot inmutable y aprobado de una política. Define como mínimo:

- motivo y objetivo;
- grace period;
- pasos y demoras;
- límite de contactos;
- horarios y zona horaria;
- ventana de validez;
- condiciones de entrada, salida y reemplazo;
- prioridad;
- límites de frecuencia;
- modalidad de contenido por canal;
- templates aprobados cuando corresponda.

Una edición crea una versión nueva. No modifica secuencias activas.

### 3.3. Instancia de secuencia

Aplicación de una versión de política a un caso concreto. Registra:

- caso y conversación asociados;
- política y versión fijadas;
- motivo;
- paso completado;
- estado operativo;
- resultado comercial;
- timestamps y causa terminal.

Semántica recomendada de `current_step`:

> ordinal del último paso completado autoritativamente; `0` significa que ningún
> paso fue completado.

Los retries técnicos del mismo paso no avanzan `current_step`.

### 3.4. Acción programada

Materialización durable de la próxima evaluación. Debe incluir:

- secuencia y paso;
- intención tipada;
- `due_at`, `expires_at` y `next_attempt_at`;
- prioridad;
- ancla conversacional o de negocio;
- condición de vigencia tipada;
- versiones esperadas;
- clave de idempotencia;
- estado técnico;
- lease y fencing token;
- resultado y `reason_code`;
- referencias externas mínimas.

### 3.5. Intento de ejecución

Cada claim produce un intento auditable con:

- worker y lease;
- inicio y fin;
- fase alcanzada;
- resultado técnico;
- error sanitizado;
- referencia al efecto remoto;
- indicación de resultado cierto o incierto.

El historial de intentos no debe perderse cuando cambia el estado actual de la
acción.

### 3.6. Eventos de auditoría

Registro append-only de hechos como:

```text
policy.published
sequence.started
sequence.paused
sequence.completed
action.scheduled
action.claimed
action.deferred
action.cancelled
message.accepted
message.delivery_unknown
human_takeover.detected
purchase.detected
```

Se guardarán IDs opacos, versiones, hashes y metadatos mínimos; no cuerpos
completos de mensajes ni PII innecesaria.

## 4. Ciclo de vida de recuperación sin respuesta

### 4.1. Abandono recibido

Un webhook Hotmart autenticado y deduplicado crea o resuelve:

- el contacto interno;
- el caso de recuperación;
- la versión de política aplicable;
- la instancia de secuencia;
- la primera acción con vencimiento al final del grace period.

El webhook no autoriza el mensaje.

La creación de caso, secuencia y primera acción debe ocurrir mediante una única
operación transaccional e idempotente de Postgres, expuesta al bridge como RPC.
El evento sólo puede considerarse planificado después de ese commit. El retry de
la misma intención debe devolver la planificación existente, no duplicarla.

Invariantes mínimas:

- un caso por evento de abandono deduplicado;
- una secuencia activa por caso;
- una acción comercial viva por caso;
- una clave idempotente por transición y paso;
- ningún webhook marcado como planificado sin caso, secuencia y primera acción.

```text
case.status = grace_period
sequence.status = active
next_action = evaluate_first_touch
```

### 4.2. Durante el grace period

Para el MVP, la compra sólo podrá considerarse confirmada mediante un evento de
compra Hotmart autenticado, deduplicado, persistido y correlacionado de forma
inequívoca con el caso. El receptor actual sólo soporta abandono: incorporar y
validar ese segundo contrato es un prerrequisito del motor.

La correlación exacta —transacción/checkout, producto, oferta e identidad— debe
definirse a partir del payload real de compra. Una coincidencia ambigua no cierra
el caso automáticamente. Sin API de lectura autoritativa, la guarda significa
“no existe una compra confirmada conocida hasta este checkpoint”, no prueba la
ausencia absoluta de una compra concurrente.

Una compra así confirmada completa el objetivo antes de contactar:

```text
case = won
sequence = completed / purchase_detected
action = cancelled / purchase_detected
```

Opt-out, identidad no resoluble, bloqueo o caso incompatible cancelan o escalan
según la causa. Una falla temporal de una fuente autoritativa difiere la acción;
no se interpreta como ausencia de datos.

Un conflicto de identidad —por ejemplo email y teléfono que resuelven contactos
distintos, o divergencia entre Supabase y Chatwoot— produce
`identity_conflict`. Es bloqueante para mensajería proactiva y requiere
reconciliación o intervención humana; nunca se elige un destinatario por
precedencia silenciosa.

### 4.3. Fin del grace period

El dispatcher reclama la acción. El worker vuelve a verificar:

- compra;
- identidad y destino;
- permisos y opt-out;
- casos y objetivos competidores;
- estado de Chatwoot;
- human takeover;
- horario y frecuencia;
- política y versiones esperadas;
- modalidad permitida por el canal.

Si corresponde, Hermes redacta o completa placeholders. El bridge valida y envía
mediante `MessageSender`.

### 4.4. Primer contacto aceptado

El primer paso sólo se completa cuando Chatwoot devuelve un mensaje canónico
válido y compatible con la solicitud.

```text
action = accepted_by_chatwoot
sequence.current_step = 1
case = active
```

A continuación se crea una única acción `evaluate_no_reply` cuyo reloj parte del
momento de aceptación del mensaje, no del horario inicialmente planeado.

El primer contacto proactivo debería converger a este mismo pipeline durable. La
implementación actual directa se considera una transición, no un segundo motor
permanente.

### 4.5. Respuesta antes del follow-up

Un inbound posterior al mensaje ancla provoca:

```text
action = cancelled / inbound_after_anchor
sequence = completed / responded
case = active
```

La secuencia de no respuesta termina, pero el caso comercial continúa en el flujo
conversacional normal.

Si el webhook de entrada no aplicó la cancelación, la validación final del worker
la detectará antes del envío.

### 4.6. Sin respuesta al vencimiento

El worker verifica específicamente:

> no existe inbound posterior al mensaje ancla y el objetivo continúa vigente.

Si corresponde, ejecuta el follow-up y materializa sólo el próximo paso. Cada
follow-up usa una nueva idempotency key; los retries del mismo follow-up conservan
la misma clave.

### 4.7. Secuencia agotada

Cuando no quedan pasos:

```text
sequence = completed / attempts_exhausted
case = sequence_exhausted
```

No se crean más acciones automáticas. Esto no equivale necesariamente a
`lost`: un operador o evento futuro puede reclasificar el caso, pero una nueva
automatización necesita una decisión explícita y una nueva instancia.

### 4.8. Compra en cualquier momento

Una compra autoritativa tiene prioridad sobre cualquier timer:

- marca el caso como ganado;
- completa la secuencia por conversión;
- cancela acciones vivas;
- invalida nuevos mensajes cuando el evento fue observado antes de iniciar el
  efecto externo;
- conserva auditoría.

El worker vuelve a consultar la proyección de eventos Hotmart inmediatamente
antes de enviar. Se conserva la ventana de carrera documentada en 2.3.

## 5. Eventos que modifican una secuencia

### 5.1. Human takeover

Según ADR-0002:

- pausa la automatización;
- cancela o invalida acciones pendientes;
- añade `automation_paused` en Chatwoot;
- no invoca Hermes.

Quitar la etiqueta no resucita acciones. Reanudar requiere releer el estado,
crear una nueva planificación y dejar trazabilidad de la anterior.

### 5.2. Opt-out o `do_not_contact`

Es terminal para toda automatización de contacto compatible con ese permiso, no
sólo para la secuencia actual. Cancela acciones competidoras del contacto y tiene
precedencia sobre reanudaciones y políticas.

`No opted out` no equivale a permiso. El MVP requiere un registro canónico de
consentimiento o base de contacto con, como mínimo:

- canal y propósito autorizados;
- fuente y evidencia;
- timestamp y jurisdicción aplicable;
- vigencia o expiración;
- motivo de revocación.

Supabase será canónico para esta autorización propia del producto; Chatwoot
seguirá siendo canónico para el contacto conversacional y sus bloqueos visibles.
Ante divergencia se aplica la condición más restrictiva y se reconcilia.

### 5.3. Contacto solicitado para una fecha

Una frase como “escribime el viernes” representa una intención nueva, anclada a
ese compromiso. Para el MVP:

- termina o reemplaza la secuencia de no respuesta;
- crea una nueva secuencia explícita `contact_requested`;
- cualquier cambio posterior de fecha reemplaza la acción anterior;
- un contacto anticipado o una nueva conversación obliga a reevaluar.

### 5.4. Compromiso de pago o decisión

Una intención como “mañana pago” no debe tratarse como simple no respuesta. Se
crea una intención `payment_pending` o `prospect_commitment`, cuya vigencia depende
de la fuente autoritativa de pago y del compromiso conversacional ancla.

Sin integración autoritativa con el proveedor correspondiente, el sistema debe
escalar o fallar cerrado; no inferirá cumplimiento sólo por texto.

### 5.5. Compromiso del agente o humano

“Te envío la propuesta mañana” puede requerir una tarea interna antes de enviar
un mensaje. El MVP no automatizará tareas internas genéricas: deberá escalarla o
representarla como alerta operativa. Se difiere una abstracción futura de
`human_task` o `integration_check`.

### 5.6. Cambio de política

Una nueva versión publicada no altera secuencias activas. Migrarlas requiere una
operación explícita que:

- invalide acciones existentes;
- vuelva a validar el caso;
- registre versión anterior y nueva;
- materialice una nueva próxima acción.

## 6. Arbitraje y límites globales

### 6.1. Una próxima acción por caso

El MVP conserva una secuencia activa y una próxima acción viva por caso. Esta
restricción evita cadencias superpuestas.

Una acción técnica de reconciliación puede necesitar coexistir en el futuro, por
lo que no debería confundirse para siempre con una acción comercial.

### 6.2. Coordinación por conversación y contacto

Dos casos diferentes pueden referirse a la misma persona. Si la operación futura
demuestra solapamientos relevantes, una versión posterior podrá incorporar una
llave de concurrencia y política global por conversación/contacto para:

- evitar dos mensajes simultáneos;
- aplicar límites de frecuencia;
- resolver prioridad;
- combinar o reemplazar objetivos incompatibles;
- impedir que dos productos disparen cadencias independientes cercanas.

El arbitraje global entre casos queda diferido para el MVP. Se mantienen sólo las
guardas existentes ante otro caso abierto claramente incompatible; no se
incorporan ranking, combinación de mensajes ni límites globales sofisticados.

### 6.3. Abandonos repetidos

Regla inicial propuesta:

- el mismo evento se deduplica por su ID externo;
- un nuevo abandono del mismo producto/oferta con un caso abierto no dispara una
  cadencia paralela: actualiza el contexto y exige reevaluación;
- un abandono de otro producto puede crear otro caso; el MVP no agrega arbitraje
  global más allá de las guardas actuales;
- una conversación activa o una secuencia incompatible bloquea un nuevo primer
  contacto automático y deriva la decisión al flujo existente;
- un caso terminal no se reactiva silenciosamente; un nuevo abandono crea una
  intención nueva según política.

La clave exacta para considerar “mismo producto/oferta” y el período de
agrupación pertenecen a la política y necesitan aprobación.

### 6.4. Prioridad de guardas

Orden recomendado:

1. opt-out, bloqueo o restricción legal;
2. compra/conversión confirmada;
3. human takeover o pausa explícita;
4. identidad o estado canónico no verificable;
5. objetivo reemplazado o versiones obsoletas;
6. conversación nueva posterior al ancla;
7. límites de frecuencia y canal;
8. horarios;
9. ejecución del paso.

Las prioridades son determinísticas y no dependen de Hermes.

## 7. Horarios, zona horaria y WABA

### 7.1. Horarios

La versión de política define:

- zona horaria del negocio o destinatario según regla aprobada;
- días y franja permitidos;
- comportamiento fuera de horario;
- expiración del paso.

Fuera de horario se difiere a la próxima ventana válida. No se considera human
takeover ni retry técnico.

### 7.2. Restricciones WABA

Cada paso debe declarar una modalidad permitida:

```text
freeform_if_window_open
approved_template_required
human_only
```

El bridge calcula cuál corresponde a partir del canal y de la ventana canónica
**antes** de invocar Hermes. En modo libre, Hermes puede proponer texto; en modo
template sólo entrega placeholders validados. Hermes no inventa templates. Fuera
de la ventana aplicable, sólo se ejecuta un template aprobado y versionado; si no
existe, la acción se omite o escala.

Esta modalidad dual para WABA propone superseder la sección de contrato único de
ADR-0004 de mensajería y requiere aprobación explícita.

Los límites de frecuencia y costo forman parte de la política de autorización,
no sólo del contenido.

## 8. Scheduler, claims y concurrencia

### 8.1. Fuente durable

`scheduled_actions` es la fuente de verdad. El sistema busca siempre:

```text
due_at <= now()
```

Así recupera acciones vencidas durante caídas o despliegues.

### 8.2. Pollers equivalentes

El scheduler es un componente lógico, pero puede tener varias réplicas. Reclama
batches mediante una operación transaccional de Postgres equivalente a
`FOR UPDATE SKIP LOCKED`.

Con PostgREST, esta operación debe encapsularse en una función RPC autorizada; no
puede implementarse de forma segura como SELECT y PATCH separados.

### 8.3. Lease y fencing

El claim asigna:

- `lease_owner`;
- `lease_expires_at`;
- token o generación monotónica;
- número de intento.

Si el worker muere, otro recupera la acción después del vencimiento. Toda
actualización final exige que el token continúe vigente, impidiendo que un worker
viejo modifique una acción reclamada nuevamente.

Esto protege el estado de Postgres, pero no puede retirar un request HTTP que un
worker viejo ya haya iniciado. Inmediatamente antes del POST se vuelve a validar
el lease y se reserva atómicamente la intención en el ledger; aun así, la
deduplicación del efecto externo es best-effort si Chatwoot no ofrece una clave
idempotente nativa verificable.

La transacción no permanece abierta mientras se llama a Hermes o Chatwoot.

### 8.4. Papel de Cron y Queues

Supabase Cron puede:

- despertar un dispatcher cuando no exista un loop permanente;
- reconciliar leases;
- supervisar backlog;
- ejecutar mantenimiento periódico.

No almacenará un job por acción. `cron_job_id` no será fuente de verdad y debería
retirarse o quedar como referencia opcional del adaptador.

Supabase Queues es un escalón futuro para desacoplar dispatch de ejecución y
absorber bursts. Los timers, cancelaciones y resultados siguen siendo canónicos
en `scheduled_actions`.

No se incorpora Celery, Redis, RQ ni Temporal en el MVP.

El primer despliegue puede operar con un único worker y claim transaccional. Los
campos de lease siguen siendo necesarios para recuperar crashes. Múltiples
pollers y fencing completo se activan cuando exista concurrencia real; no son un
requisito de despliegue inicial.

## 9. Idempotencia, fallos y reconciliación

### 9.1. Semántica real

El sistema ofrece:

> procesamiento al menos una vez, deduplicación interna y reconciliación
> best-effort del efecto externo.

No promete exactly-once entre Postgres y Chatwoot.

### 9.2. Clave de idempotencia

Una intención reutiliza la misma clave en todos sus retries. Reprogramar por un
nuevo objetivo o paso crea una clave diferente.

Además de la unicidad en `scheduled_actions`, debe existir un ledger durable de
efectos salientes para evitar dos solicitudes internas con la misma intención.

La reserva de ese ledger y la transición de la acción deben ser una única
operación atómica. Esta protección evita duplicados internos, pero no demuestra
exactly-once en Chatwoot.

### 9.3. Resultado remoto incierto

Si ocurre un timeout después de iniciar el envío:

```text
action = delivery_unknown
```

No se reenvía automáticamente. Primero se espera un intervalo definido y se
consulta de forma paginada el tramo de conversación posterior al ancla usando un
marcador único de la intención. La reconciliación tiene un máximo de intentos y
una ventana temporal. El resultado puede ser:

- reconciliado como aceptado;
- reconciliado como no aplicado sólo cuando el contrato observado permita
  demostrarlo con suficiente certeza;
- escalado por imposibilidad de determinarlo.

Para el MVP, si no puede demostrarse que el envío no ocurrió, no hay retry
automático: se escala. La ausencia inmediata del mensaje no es prueba suficiente.

### 9.4. Retry técnico

Los retries técnicos conservan intención, paso e idempotency key. Usan
`next_attempt_at` separado de `due_at`, con backoff y límite.

Agotar retries no equivale a agotar pasos comerciales.

### 9.5. Fallo del contexto autoritativo

Si Chatwoot, Hotmart/Supabase u otra fuente necesaria no puede consultarse o
validarse, el sistema difiere o escala. Nunca interpreta incertidumbre como
permiso.

## 10. Estados canónicos

### 10.0. Caso

```text
grace_period -> active | won | cancelled | expired | escalated
active -> won | lost | cancelled | expired | escalated | sequence_exhausted
```

`sequence_exhausted` significa que terminó la cadencia automática sin respuesta
o compra; no equivale a una falla técnica. Los estados terminales no se reabren.
Un abandono posterior crea una intención nueva según política.

### 10.1. Secuencia

```text
active -> paused | completed | cancelled | failed
paused -> active | cancelled | failed
```

Los estados terminales no se reabren. Una nueva intención crea una nueva
instancia.

El estado técnico se acompaña con resultado comercial, por ejemplo:

```text
responded
purchased
booked
payment_confirmed
handed_off
opted_out
unreachable
attempts_exhausted
superseded
```

### 10.2. Acción

El estado durable de la acción se separa del lease y de la fase de cada intento.

Estado inicial:

```text
pending
```

Resultados terminales:

```text
accepted_by_chatwoot
skipped
cancelled
expired
permanent_failed
superseded
```

Resultados no terminales o recuperables:

```text
deferred
retryable_failed
delivery_unknown
```

`accepted_by_chatwoot` no significa `delivered`. El envío del canal, entrega,
lectura y fallo de entrega son estados posteriores del mensaje. La primera
cadencia correrá desde aceptación por Chatwoot; una política futura podrá exigir
otro ancla sólo si el canal expone ese evento de forma confiable.

El claim vive en `lease_owner`, `lease_expires_at` y generación. Las fases
`validating`, `generating` y `sending` pertenecen al intento de ejecución, no al
estado durable de la acción.

### 10.3. Efecto de cada decisión

| Decisión | Acción | Secuencia | Caso |
|---|---|---|---|
| `execute` aceptado | terminal `accepted_by_chatwoot` | avanza el paso y crea la próxima acción | permanece activo salvo resultado comercial |
| `defer` | misma acción con `next_attempt_at` o `due_at` según causa | no avanza | no cambia |
| `retryable_failed` | misma intención e idempotencia con backoff | no avanza | no cambia |
| `delivery_unknown` | bloqueada para retry automático y reconciliada | no avanza hasta resolver | queda bajo observación |
| `skip` | terminal con reason code | completa, reemplaza o continúa según regla tipada del paso | puede no cambiar |
| `replace` | acción vieja `superseded` y nueva intención creada | secuencia nueva o replanteada con auditoría | objetivo actualizado |
| `cancel` | terminal | terminal `cancelled` | cambia sólo si la causa lo exige |
| `pause` | acción invalidada | `paused` | pausa operativa |
| `expire` | terminal | avanza o agota según política | puede pasar a `sequence_exhausted` |
| `escalate` | terminal o retenida según causa | `paused` o `failed` | requiere intervención humana |

El grafo exacto y los reason codes deberán aplicarse mediante APIs/RPC cerradas;
no se permitirán updates arbitrarios.

## 11. Auditoría y observabilidad

Métricas mínimas:

- acciones pendientes y vencidas;
- antigüedad de la acción vencida más vieja;
- demora entre `due_at`, claim y ejecución;
- claims vencidos;
- retries por acción;
- resultados inciertos;
- cancelaciones por respuesta, compra, takeover y opt-out;
- secuencias agotadas;
- errores por canal;
- duplicados evitados;
- límites de frecuencia aplicados.

Se requiere un watchdog para detectar:

- webhooks persistidos sin avance;
- acciones reclamadas con lease vencido;
- acciones en `delivery_unknown`;
- casos activos sin secuencia o próxima acción cuando deberían tenerla;
- secuencias activas cuya política publicada no puede resolverse.

## 12. Alcance recomendado del MVP

Incluido:

- recuperación de carrito;
- grace period configurable;
- primer contacto por el pipeline durable;
- seguimiento lineal por falta de respuesta;
- una secuencia y una próxima acción por caso;
- cancelación por respuesta, compra, opt-out o takeover;
- horarios y frecuencia básicos;
- claims recuperables;
- idempotencia y reconciliación;
- auditoría mínima;
- políticas manualmente aprobadas y versionadas.

Despliegue inicial mínimo:

- una política publicada manualmente;
- una secuencia lineal;
- un worker con claim transaccional y recuperación de lease;
- intentos auditables mínimos;
- reconciliación conservadora de mensajes inciertos.

Múltiples pollers, migración de políticas activas y auditoría ampliada se activan
cuando exista necesidad operativa demostrada.

Diferido:

- múltiples obligaciones simultáneas;
- tareas humanas genéricas;
- workflows en grafo;
- cadencias multicanal;
- calendario empresarial sofisticado;
- A/B testing;
- optimización dinámica por IA;
- migración automática de políticas;
- Temporal u otro workflow engine.

## 13. Relación con el esquema actual

El esquema existente constituye una buena base, pero antes de implementar debe:

- incorporarse al repositorio como migración baseline;
- agregar entidades de política y versiones;
- definir `current_step` como último paso completado;
- separar pasos comerciales de retries técnicos;
- agregar `sequence_step` y versión esperada de secuencia a la acción;
- agregar lease owner, expiración y fencing;
- agregar `next_attempt_at` y `delivery_unknown`;
- definir mapping inequívoco con IDs de Chatwoot;
- revisar `cron_job_id`;
- separar auditoría append-only de proyecciones eliminables;
- definir el grafo de estados y reason codes;
- separar “webhook procesado” de “caso o acción completados”.
- reemplazar una supuesta `conversation_version` remota por checkpoints
  observados: mensaje ancla, último mensaje, estado, etiquetas y timestamp;
- agregar contrato canónico de consentimiento y correlación de compras.

La colisión histórica de dos ADR con número `0004` quedó resuelta renumerando el
ADR de empaquetado reproducible como ADR-0005 antes de aceptar ADR-0007.

## 14. Decisiones revisadas

### D0. Autoridad comercial y supersesión de ADR-0003

**Estado:** aprobada por el usuario el 2026-08-02.

**Decisión:** las guardas y la decisión ejecutable permanecen en el bridge;
Hermes conserva juicio de estrategia y redacción sólo entre alternativas seguras.
ADR-0007 declara qué secciones de ADR-0003 quedan supersedidas.

### D1. Pipeline único para todo contacto proactivo

**Estado:** aprobada por el usuario el 2026-08-02.

**Decisión:** incluir tanto el primer contacto como los follow-ups en el motor
durable. El primer caso de uso que se implementará sobre el nuevo motor será la
migración del primer contacto existente. Sólo después de validarlo se agregarán
los pasos de follow-up. El flujo E2E actual se conserva como referencia de
comportamiento durante la migración.

### D2. Alcance inicial del motor

**Estado:** aprobada por el usuario el 2026-08-02.

**Decisión:** diseñar políticas y componentes principales de forma reutilizable,
pero implementar primero sólo recuperación de carrito: primer contacto durable y
seguimiento por falta de respuesta. No convertir todavía `recovery_cases` en un
CRM o workflow universal; las extensiones se justificarán con nuevos casos de uso
reales.

### D3. Cardinalidad del MVP

**Estado:** parcialmente aceptada y diferida por el usuario el 2026-08-02.

**Decisión:** cada caso mantendrá una sola secuencia activa y una sola próxima
acción comercial, como simplificación interna del motor. No se diseñará ni
implementará en el MVP un arbitraje global entre varios casos de un mismo
contacto o conversación. Esa restricción se reconsiderará sólo si la operación
real demuestra solapamientos relevantes.

### D4. Aprobación de políticas

**Estado:** aprobada por el usuario el 2026-08-02.

**Decisión:** toda versión propuesta por Automation Expert requiere validación de
la aplicación y aprobación humana explícita antes de publicarse. Una versión
publicada es inmutable; cualquier edición crea una versión nueva. La
autoaprobación se difiere hasta disponer de reglas y experiencia operativa.

### D5. Respuesta entrante

**Estado:** aprobada por el usuario el 2026-08-02.

**Decisión:** cualquier inbound posterior al mensaje ancla cancela la próxima
acción y completa la secuencia `no_reply`, pero no cierra el caso comercial. La
conversación continúa normalmente. Sólo una intención nueva y explícita puede
crear otra secuencia.

### D6. Human takeover y reanudación

**Estado:** confirmada por el usuario el 2026-08-02.

**Decisión:** takeover pausa la automatización y cancela la planificación
vigente; quitar la etiqueta no reanuda ni resucita acciones. Una reanudación
explícita vuelve a validar la conversación y crea acciones nuevas. Esto confirma
y aplica ADR-0002 al motor de seguimientos.

### D7. Conflictos entre casos

**Estado:** diferida por el usuario el 2026-08-02, junto con D3.

**Decisión:** el MVP no incluirá un sistema general de arbitraje, ranking o
combinación entre distintos casos de un mismo contacto. Se mantienen las guardas
actuales ante casos abiertos claramente incompatibles. El arbitraje global se
reconsiderará sólo si la operación demuestra solapamientos relevantes.

### D8. Cadencia comercial inicial

**Estado:** aprobada como política efímera de prueba por el usuario el 2026-08-02.

**Decisión:** la primera versión de prueba usará:

- grace period de 1 hora;
- follow-up 1 a las 24 horas del contacto anterior aceptado por Chatwoot;
- follow-up 2 a las 72 horas del contacto anterior aceptado por Chatwoot;
- máximo de 3 mensajes automáticos incluyendo el primer contacto;
- lunes a sábado de 09:00 a 19:00 en la zona horaria del negocio;
- expiración total a los 7 días desde el abandono.

Estos valores no forman parte fija del motor. Horarios, demoras, cantidad de
pasos y estrategia de mensajes serán configuración versionada y reemplazable
según el cliente. Esta política sólo ofrece un primer diseño controlado para
validar el motor.

### D9. Canal WABA

**Estado:** aprobada por el usuario el 2026-08-02.

**Decisión:** diseñar desde ahora el contrato dual. El bridge determina antes de
invocar Hermes si corresponde texto libre dentro de la ventana aplicable o un
template aprobado fuera de ella. Hermes sólo completa placeholders en modo
template y no puede inventar estructuras. WABA no se implementará hasta disponer
de inbox y templates reales. Esto supersederá parcialmente ADR-0004 de
mensajería.

### D10. Fuente y correlación de compras

**Estado:** aprobada por el usuario el 2026-08-02.

**Decisión:** aceptar como confirmación automática sólo eventos de compra Hotmart
autenticados, deduplicados y correlacionados inequívocamente con el caso. Una
correlación ambigua exige reconciliación o intervención. Antes de implementar se
investigarán y probarán el payload real, sus identificadores y los eventos de
reversión. Un reembolso o disputa posterior no reabre automáticamente la
secuencia anterior.

### D11. Consentimiento

**Estado:** aprobada por el usuario el 2026-08-02.

**Decisión:** Supabase conserva una autorización o base válida tipada por canal y
propósito, con procedencia y timestamp mínimos. Puede provenir de consentimiento,
condiciones del checkout, relación comercial u otra base configurada y validada
para el cliente. `Bloqueado` cancela; `desconocido` falla cerrado y deriva. La
ausencia de opt-out o la aprobación de un template no habilitan por sí solas el
contacto proactivo.

### D12. Repetición de abandonos

**Estado:** aprobada por el usuario el 2026-08-02.

**Decisión:** los abandonos repetidos del mismo producto y oferta se agrupan
mientras exista un caso abierto: actualizan su contexto y fuerzan reevaluación,
sin iniciar una cadencia paralela. Otro producto/oferta o un caso anterior ya
terminal pueden crear una intención y un caso nuevos. El período exacto de
agrupación podrá configurarse posteriormente; no bloquea el diseño inicial.

## 15. Criterio para una futura migración tecnológica

Postgres sigue siendo suficiente mientras el problema sea seleccionar y ejecutar
próximas acciones lineales con buen nivel operativo.

Evaluar Supabase Queues si el dispatcher acumula backlog o necesita desacoplarse
de los workers. Evaluar Temporal cuando aparezcan workflows con múltiples esperas
simultáneas, señales externas, compensaciones, aprobaciones humanas frecuentes o
cuando mantener las máquinas de estado consuma una parte significativa del
trabajo del equipo.

El volumen por sí solo no obliga a adoptar un workflow engine; la complejidad de
coordinación es la señal principal.

## 16. Documentación relacionada

- [Arquitectura](../architecture.md)
- [ADR-0001: frontera del profile comercial](../decisions/0001-commercial-profile-boundary.md)
- [ADR-0002: human takeover](../decisions/0002-human-takeover-detection.md)
- [ADR-0003: frontera determinística](../decisions/0003-deterministic-reasoning-boundary.md)
- [ADR-0004: mensajería y WABA](../decisions/0004-messaging-layer-abstraction.md)
- [ADR-0005: empaquetado reproducible](../decisions/0005-reproducible-client-deployments.md)
- [ADR-0006: superficie de tres agentes](../decisions/0006-three-agent-product-surface.md)
- [ADR-0007: motor durable de próxima acción](../decisions/0007-durable-next-action-engine.md)
- [Contrato V1 del motor](../contracts/followup-engine-v1.md)
- [Registro E2E del 2026-08-02](../operations/2026-08-02-hotmart-recovery-e2e.md)
