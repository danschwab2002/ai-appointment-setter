# Contrato conceptual V1: motor de seguimientos

- **Estado:** aceptado como contrato conceptual; pendiente de esquema físico
- **Fecha:** 2026-08-03
- **ADR:** [ADR-0007](../decisions/0007-durable-next-action-engine.md)
- **Alcance:** recuperación de carrito y primer contacto durable

Este documento define significados, invariantes y transiciones. No fija todavía
nombres de tablas, SQL, endpoints ni payloads definitivos.

## 1. Principios contractuales

1. Postgres conserva la planificación y el estado operativo canónico.
2. Chatwoot conserva la realidad conversacional y la aceptación del mensaje.
3. Hotmart conserva la realidad de abandono y compra.
4. Hermes propone estrategia o contenido, pero no autoriza efectos.
5. Toda acción vencida debe reevaluarse antes de ejecutar.
6. Sólo existe una próxima acción materializada por caso.
7. Toda política publicada es inmutable.
8. Toda transición durable es idempotente y auditable.
9. Un resultado externo incierto bloquea nuevos envíos de la secuencia.
10. Ningún contrato contiene PII innecesaria en logs o auditoría.

## 2. Política publicada

Una política define comportamiento comercial versionado.

```text
PolicyVersion
  policy_key
  version
  status
  purpose
  timezone
  business_windows
  grace_period
  expires_after
  max_automatic_messages
  steps[]
  approved_by
  approved_at
  published_at
```

### Invariantes

- `policy_key + version` es único.
- Sólo `published` puede iniciar secuencias.
- Una versión `published` no se edita.
- Cada paso declara su demora, tipo de acción y condición de vigencia.
- Horarios y tiempos son configuración, no constantes del motor.
- Hermes o Automation Expert pueden proponer; no pueden publicar.

### Política inicial de prueba

```text
purpose: cart_recovery
business timezone: tenant timezone
business window: Monday-Saturday 09:00-19:00
grace period: 1 hour
step 1: first_contact
step 2: no_reply_followup at +24 hours from the first accepted outbound message
step 3: no_reply_followup at +72 hours from the first accepted outbound message
max automatic messages: 3
sequence expiry: 7 days from abandonment
```

Los campos `steps[].delay` son **offsets absolutos** desde la primera aceptación
outbound durable de la secuencia. No son demoras relativas encadenadas desde el
mensaje anterior. Por ejemplo, `2 minutes`, `5 minutes`, `10 minutes` produce
deadlines `T+2`, `T+5`, `T+10`, donde `T` es el `accepted_at` del primer intento
aceptado. Una aceptación intermedia tardía no desplaza los pasos posteriores;
la acción vencida se materializa con su deadline original y debe atravesar una
nueva re-evaluación autoritativa antes de cualquier request externo. Los offsets
negativos son inválidos y fallan cerrados.
La base valida los offsets al insertar o cambiar `steps`; una policy con un
offset negativo o no interpretable nunca puede llegar a reserva ni a
`request_started`. La migración que introduce esta semántica también valida
transaccionalmente todas las policies existentes y aborta sin cambios parciales
si encuentra una incompatible. El finalizador conserva una comprobación
secundaria, pero no es la primera frontera de validación.

## 3. Caso comercial

Un caso representa un objetivo comercial, no una espera concreta.

```text
CommercialCase
  case_id
  purpose
  status
  source
  source_subject_key
  contact_id
  conversation_id?
  policy_key
  policy_version
  created_at
  updated_at
  terminal_reason?
  revision
```

### Estados mínimos

```text
grace_period -> active | won | cancelled | expired | escalated
active -> won | lost | cancelled | expired | escalated | sequence_exhausted
```

### Invariantes

- Una compra correlacionada inequívocamente puede mover el caso a `won`.
- Una respuesta no cierra automáticamente el caso.
- Un caso terminal no se reabre silenciosamente.
- Mismo producto/oferta con caso abierto actualiza ese caso.
- Otro producto/oferta o caso terminal puede crear un caso nuevo.

## 4. Instancia de secuencia

Una secuencia aplica una versión de política a un caso.

```text
SequenceInstance
  sequence_id
  case_id
  policy_key
  policy_version
  purpose
  status
  current_step
  anchor
  automatic_messages_accepted
  started_at
  updated_at
  completed_at?
  completion_reason?
  revision
```

### Estados mínimos

```text
active -> paused | completed | cancelled | failed
paused -> active | cancelled | failed
```

### Invariantes

- El MVP permite una secuencia activa por caso.
- La secuencia conserva su versión de política hasta terminar.
- `delivery_unknown` en un intento bloquea el avance.
- Reanudar después de human takeover crea planificación nueva.

## 5. Ancla de vigencia

El ancla identifica el hecho que una acción espera que siga vigente.

```text
ActionAnchor
  type
  subject_internal_id
  observed_at
  checkpoint
```

Tipos iniciales:

```text
cart_abandonment
accepted_outbound_message
requested_contact_time
```

Para `no_reply`, la condición inicial es:

```text
No existe un mensaje entrante posterior al accepted_outbound_message anclado.
```

El checkpoint es evidencia concreta —por ejemplo, ID interno y timestamp—, no una
promesa de snapshot distribuido sobre Chatwoot.

## 6. Acción programada

Una acción representa «reevaluar en este momento», no «enviar».

```text
ScheduledAction
  action_id
  case_id
  sequence_id
  policy_key
  policy_version
  step_key
  action_type
  due_at
  next_attempt_at?
  expires_at
  anchor
  status
  idempotency_key
  attempt_count
  lease_owner?
  lease_generation?
  lease_expires_at?
  created_at
  updated_at
  terminal_reason?
```

### Estados durables mínimos

```text
pending
deferred
retryable_failed
delivery_unknown
accepted_by_chatwoot
cancelled
skipped
expired
permanent_failed
superseded
```

El claim se representa mediante campos de lease y no necesita ser un estado de
negocio independiente.

### Invariantes

- Sólo una acción comercial viva —`pending`, `deferred`, `retryable_failed` o
  `delivery_unknown`— por secuencia.
- `idempotency_key` identifica una intención lógica estable.
- Una acción terminal nunca vuelve a `pending`.
- Diferir o reintentar conserva la misma acción e idempotency key. Un
  diferimiento comercial puede actualizar `due_at`; un retry técnico utiliza
  `next_attempt_at` con backoff.
- Sólo reemplazar el objetivo o paso crea una sucesora y marca la anterior
  `superseded`.
- Un lease expirado permite reclamar de nuevo la misma acción.
- Sólo la generación vigente puede confirmar una transición en Postgres.

## 7. Decisión de reevaluación

El bridge devuelve una decisión estructurada:

```text
ReevaluationDecision
  action_id
  decision
  reason_code
  evaluated_at
  case_revision
  sequence_revision
  conversation_checkpoint?
  replacement_due_at?
  replacement_step_key?
  escalation_type?
```

Valores permitidos:

```text
execute
defer
replace
cancel
skip
pause
expire
escalate
```

### Efectos

| Decisión | Acción actual | Secuencia | Acción sucesora |
|---|---|---|---|
| `execute` | reserva e inicia intento; todavía no terminaliza | continúa | sólo después del resultado externo |
| `defer` | misma acción con `due_at` o `next_attempt_at` nuevo | continúa | ninguna |
| `replace` | `superseded` | continúa | otro paso o intención |
| `cancel` | `cancelled` | puede completar | ninguna salvo nuevo disparador |
| `skip` | `skipped` | avanza o completa | según política |
| `pause` | `cancelled` | `paused` | ninguna automática |
| `expire` | `expired` | `completed` o `failed` según política | ninguna |
| `escalate` | `cancelled` o bloqueada | `paused` o `failed` según causa | tarea explícita de revisión |

## 8. Precedencia de guardas

El orden conceptual de reevaluación es:

```text
1. opt-out, bloqueo o restricción legal conocida
2. compra autoritativa conocida
3. human takeover o pausa
4. integridad de identidad, destino y estado canónico
5. política, step y revisiones vigentes
6. respuesta posterior al ancla
7. límites de frecuencia y modalidad de canal
8. horario y expiración
9. elegibilidad comercial
10. propuesta de Hermes
11. validación final cercana al efecto
```

Una guarda bloqueante no puede ser revocada por una decisión posterior ni por
Hermes. Las restricciones globales ya conocidas —por ejemplo, opt-out— se aplican
aunque otra validación falle. Si no puede establecerse con certeza a qué persona
corresponde una restricción, la incertidumbre tampoco habilita el envío.

### Resultados típicos

| Hecho actual | Resultado |
|---|---|
| compra correlacionada | cancelar acción, completar secuencia, ganar caso |
| opt-out | cancelar acción y automatización aplicable |
| human takeover | cancelar acción y pausar secuencia |
| respuesta posterior al ancla | cancelar acción y completar `no_reply` |
| permiso desconocido | escalar sin enviar |
| fuera de horario | diferir la misma acción hasta la próxima ventana |
| identidad conflictiva | escalar sin enviar |
| política o step obsoletos | reemplazar o cancelar |

## 9. Intento de efecto externo

Cada ejecución saliente crea evidencia durable antes del request.

```text
DeliveryAttempt
  attempt_id
  action_id
  idempotency_key
  attempt_number
  channel
  mode
  started_at
  outcome
  remote_message_id?
  accepted_at?
  reason_code?
  reconciliation_deadline?
```

Modos de contenido:

```text
freeform
approved_template
```

Resultados mínimos:

```text
accepted_by_chatwoot
rejected
failed_before_request
delivery_unknown
```

### Invariantes

- `accepted_by_chatwoot` no significa entregado ni leído.
- `accepted_by_chatwoot` sólo puede persistirse mediante `record_and_finalize_followup_acceptance`, junto con conversación, mensaje outbound público y correlación canónicos; los RPC genéricos rechazan accepted.
- `accepted_message_id` referencia un mensaje real y no puede aceptar dos intentos.
- Una aceptación real se conserva aunque cambie la autoridad durante el request; en ese caso no avanza la secuencia ni crea sucesor.
- La conversación aceptada se vincula atómicamente a `recovery_cases.conversation_id` y a la secuencia; una asociación contradictoria dentro del mismo caso falla cerrada.
- `channel_identities.external_conversation_id` es sólo un puntero denormalizado last-write-wins a la conversación más reciente de la identidad y nunca se usa como autoridad de respuesta o seguimiento.
- La conversación autoritativa debe pertenecer al mismo contacto y a la identidad seleccionada del caso.
- `first_contact_review` puede crear una conversación Chatwoot nueva; `no_reply_review` publica exclusivamente en la conversación canónica ya vinculada al caso. Un seguimiento nunca busca/crea contacto ni abre otra conversación.
- La correlación remota de un seguimiento es `sha256("followup:{attempt_id}")`; no se etiqueta como primer contacto.
- Un request ambiguo produce `delivery_unknown`.
- `delivery_unknown` no se reintenta automáticamente.
- La reconciliación busca un marcador estable durante una ventana acotada.
- Si no puede probarse aplicado o no aplicado, requiere intervención.
- `ALLOWED_WHATSAPP_JID` sigue siendo obligatorio durante pruebas.

## 10. Operaciones transaccionales conceptuales

### 10.1. Planificar recuperación

Entrada:

```text
authenticated abandonment event
resolved identity
published policy version
```

Efecto atómico:

```text
crear o reutilizar channel_identity WhatsApp canónica para el JID autorizado
crear o actualizar caso
seleccionar esa identidad y marcar identity_resolution_status=resolved
crear secuencia si corresponde
crear primera acción
registrar evento de auditoría
```

Repetir la misma entrada no crea una segunda intención lógica.
Para el destinatario allowlisted, identidad y plan se materializan mediante un
único RPC y un único commit; el webhook sólo pasa a `processed` después de ese
commit. No puede quedar visible un plan enviable sin identidad seleccionada.

### 10.2. Reclamar acciones vencidas

Entrada:

```text
worker id
now
lease duration
batch size
```

Efecto atómico:

```text
seleccionar pending/deferred/retryable_failed cuya fecha elegible <= now
usar next_attempt_at para retry técnico y due_at para evaluación comercial
ignorar leases vigentes
asignar owner, generation y expiry
retornar acciones reclamadas
```

`delivery_unknown` no vuelve al dispatcher normal: lo procesa la reconciliación.

### 10.3. Aplicar reevaluación o reservar intento

Entrada:

```text
action id
lease generation
expected revisions
decision
```

Efecto atómico:

```text
verificar lease y revisiones
si decision != execute:
  aplicar la transición correspondiente
  conservar la misma acción para defer
  crear una sucesora sólo para replace
si decision == execute:
  reservar ledger e intento con idempotency key estable
  conservar la acción no terminal hasta conocer el resultado externo
registrar auditoría
```

### 10.4. Confirmar resultado externo

Entrada:

```text
action id
attempt id
lease generation
delivery outcome
remote message id?
```

Efecto atómico:

```text
accepted_by_chatwoot:
  crear o reutilizar conversación y mensaje canónicos sin carrera
  vincular la conversación a la identidad de canal
  terminalizar acción como accepted_by_chatwoot
  si la autoridad sigue vigente, avanzar current_step una sola vez
  si la autoridad cambió, conservar evidencia sin avanzar ni crear sucesor
  crear como máximo la próxima acción comercial, anclada al mensaje y conversación canónicos
failed_before_request o rejected:
  marcar retryable_failed con next_attempt_at o permanent_failed según política
delivery_unknown:
  marcar action e intento delivery_unknown
  bloquear avance y retry automático
registrar auditoría
```

### 10.5. Reconciliar resultado incierto

```text
mensaje encontrado inequívocamente:
  finalizar como accepted_by_chatwoot y avanzar una sola vez
no aplicado demostrado por el contrato observado:
  marcar retryable_failed con next_attempt_at
resultado todavía indeterminado al vencer la ventana:
  conservar evidencia delivery_unknown
  pausar la secuencia y escalar
```

### 10.6. Registrar respuesta entrante

Efecto esperado:

```text
persistir checkpoint conversacional
cancelar acción no_reply posterior al ancla
completar secuencia con reason = replied
mantener caso comercial abierto
```

La reevaluación previa al envío repite esta guarda por si el webhook llegó tarde o
falló.

### 10.7. Registrar compra

Efecto esperado:

```text
persistir evento autenticado y deduplicado
correlacionar inequívocamente
marcar caso won
completar secuencia con completion_reason = purchase_detected
cancelar acción pendiente
registrar auditoría
```

Una correlación ambigua no ejecuta el cierre automático y pausa de forma
fail-closed los casos candidatos. El ingreso, la correlación y sus outcomes se
definen en [Compra aprobada de Hotmart V1](hotmart-purchase-approved-v1.md).

## 11. Autoridad de fuentes

| Hecho | Fuente autoritativa |
|---|---|
| realidad de abandono, compra y reversión conocida por el motor | eventos autenticados de Hotmart persistidos y correlacionados |
| contactos, IDs y orden de conversaciones, inbox, asignación, etiquetas, mensajes, timestamps y capacidad actual de responder | Chatwoot |
| planificación y política | Supabase/Postgres |
| permiso de contacto | estado estructurado en Supabase con procedencia |
| autorización ejecutable | bridge |
| estrategia y redacción | Hermes dentro de límites |
| aceptación del mensaje | respuesta o reconciliación contra Chatwoot |

La autoridad del proveedor y la proyección conocida por el motor no son lo mismo.
El motor sólo puede actuar sobre eventos Hotmart autenticados y persistidos y
sobre checkpoints de Chatwoot obtenidos correctamente. «No hay compra/respuesta
conocida hasta este checkpoint» no prueba que el hecho no esté ocurriendo en ese
instante.

Incluso después de la validación final existe una ventana residual entre leer el
estado y completar el request externo. El contrato reduce esa carrera mediante
reevaluación cercana al efecto; no promete eliminarla.

### Baja inbound durable implementada

Una baja explícita en un batch canónico de Chatwoot se admite mediante
`apply_chatwoot_inbound_opt_out`. La evidencia se identifica por cuenta, inbox,
conversación y mensaje canónicos; el texto no se persiste en auditoría. La RPC:

Antes de consultar o aplicar el stop, el bridge obtiene la conversación canónica
por API y exige que pertenezca al inbox configurado y al JID autorizado. Los
identificadores declarados por el webhook no sustituyen esa autoridad.

- serializa la admisión y `request_started` por cuenta + usuario externo, y
  consulta todo stop anterior antes de permitir la frontera ejecutable;
- deja `contacts` en `opted_out/do_not_contact` y una autorización `denied` activa;
- cancela casos, secuencias, acciones e intentos que no cruzaron
  `request_started`;
- conserva un intento iniciado como `delivery_unknown`, con reconciliación y sin
  retry ciego;
- una evidencia tardía `not_applied` mueve ese intento a `rejected` y la acción a
  `cancelled`, sin crear retry ni sucesor;
- persiste `unmatched` o `ambiguous` como stop fact de conversación y permite
  reconciliarlo después sin duplicar evidencia;
- impide relajar físicamente el stop o borrar su denial sin un contrato futuro de
  reautorización explícita.

`service_role` conserva lectura pero no DML directo sobre
`followup_delivery_attempts`. Reserva, request-start, finalización y
reconciliación atraviesan entrypoints `SECURITY DEFINER`; sus helpers internos no
son ejecutables por roles de API. Este cierre evita reescribir un estado terminal
desde PostgREST.

La señal operacional en Chatwoot es una proyección durable separada. Un worker
con lease comprueba primero las labels canónicas: si ya existen
`automation_opted_out` y `automation_paused`, no vuelve a ejecutar el macro. Sólo
marca `applied` después de confirmar ambas. La finalización exige lease no nulo,
vigente, del mismo owner y generación; un replay tardío no incrementa intentos ni
muta el evento. Los fallos usan backoff acotado y terminan en `dead_letter`;
nunca revierten la baja SQL.

## 12. Auditoría mínima

Cada transición registra:

```text
event_type
occurred_at
actor_type
internal case/sequence/action ids
policy key/version
from_status
to_status
reason_code
attempt id?
lease generation?
```

No registra teléfonos, emails, nombres, contenido, payloads completos, tokens,
firmas ni URLs de búsqueda con PII.

## 13. Alcance V1 y aspectos diferidos

Incluido:

- primer contacto durable;
- grace period;
- no respuesta;
- dos follow-ups configurables;
- compra, respuesta, opt-out y human takeover;
- horario de negocio;
- claims recuperables;
- resultado remoto incierto;
- política publicada e inmutable.

Diferido:

- arbitraje global entre casos;
- frecuencia global sofisticada por contacto;
- múltiples pollers salvo necesidad real;
- WABA hasta tener inbox y templates;
- otros propósitos fuera de recuperación de carrito;
- migración automática entre versiones;
- workflow engine genérico.

## 14. Contratos físicos todavía pendientes

Antes de implementar deben definirse y probarse:

1. baseline versionado del esquema Supabase actual;
2. payload, identificadores y correlación de compra Hotmart, además de los
   contratos reales de reembolso, disputa y otras reversiones;
3. esquema físico de autorización por canal y propósito;
4. firmas concretas de RPC y constraints SQL;
5. marcador y límites reales de reconciliación Chatwoot;
6. códigos de razón y métricas operativas definitivos.

Estos puntos concretan este contrato; no reabren las decisiones de ADR-0007 salvo
que la investigación demuestre una incompatibilidad real.

## 15. Verificación ejecutable

El harness rápido aplica baseline y migración sobre PGlite:

```bash
cd tests/sql/followup_engine
npm test
```

La carrera de aceptación canónica debe verificarse además contra una base
PostgreSQL real, vacía y descartable:

```bash
ALLOW_DISPOSABLE_DATABASE=followup-concurrency \
DATABASE_URL=postgresql://.../followup_concurrency_probe \
PSQL=/ruta/a/psql npm run test:real-postgres
```

La prueba exige dos backends identificados y activos simultáneamente, observa
al menos una espera de lock real y comprueba la serialización por contacto: la
primera llamada materializa la aceptación y la segunda reproduce el mismo
resultado canónico. La evidencia relacional exige una sola conversación, un
solo mensaje accepted vinculado al intento, una sola identidad de canal y un
solo sucesor `no_reply_review` anclado al mensaje y la conversación canónicos.
No afirma una carrera de inserts posterior al lock, porque el orden global de
locks la evita para el mismo contacto. El script rechaza bases con objetos en
esquemas de usuario, exige confirmación explícita y sólo acepta nombres de base
con prefijo `followup_concurrency`.
