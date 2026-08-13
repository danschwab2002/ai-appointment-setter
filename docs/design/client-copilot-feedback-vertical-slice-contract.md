# Contrato técnico propuesto — primer corte vertical de feedback

- **Estado:** Propuesta aceptada; implementación parcial del Corte A
- **Versión conceptual:** `daily-owner-feedback-v1`
- **Implementación:** Corte A (`create_review_batch` fixture-only) implementado; resto no implementado
- **Alcance:** Lote manual con fixtures sanitizados, revisión de un ítem por vez, feedback confirmado, cambio candidato y entrega durable simulada
- **Fuera de alcance:** Scheduler diario, conversaciones reales, publicación de releases, profile completo del Copilot y conectores productivos
- **Fuente:** [Ciclo diario de feedback del Client Copilot](client-copilot-daily-feedback-cycle-mvp.md)
- **Contrato implementado:** [Creación de lote diario con fixtures](../contracts/daily-owner-feedback-v1.md)

## 1. Objetivo

Este documento transforma el diseño del ciclo diario en un contrato suficientemente preciso para implementar un primer tracer bullet con TDD.

El corte debe demostrar:

```text
crear lote manual
→ materializar fixtures sanitizados
→ reclamar sesión de revisión
→ presentar un ítem mediante una entrega durable simulada
→ registrar decisión
→ conservar feedback literal
→ confirmar interpretación
→ clasificar
→ crear cambio candidato
→ reanudar sin duplicar
→ cerrar lote
```

No es todavía un contrato público implementado. Al aceptarse y comenzar la implementación, sus schemas definitivos deben trasladarse a `docs/contracts/` y mantenerse alineados con código, migraciones y pruebas.

## 2. Frontera y actores

### 2.1. Actores

- **Operador:** crea el lote manual del corte controlado.
- **Revisor:** identidad autorizada que juzga las conversaciones.
- **Client Copilot:** propone resumen, interpretación y clasificación mediante salidas estructuradas; no muta estado directamente.
- **Servicio de revisión:** valida comandos, autorización, fencing, estados e idempotencia y persiste transiciones.
- **Worker de entrega:** reserva y finaliza efectos hacia un conector simulado.
- **Compilador de releases:** futuro consumidor de cambios candidatos; no forma parte de este corte.

### 2.2. Suposiciones del corte

- Un único tenant y un único alcance de prueba, aunque todas las identidades permanecen explícitas.
- Un único revisor autorizado.
- Fixtures ficticios y sanitizados, sin PII real.
- Reloj y IDs inyectables en pruebas.
- Conector stateful simulado con resultados aceptado, rechazado y ambiguo.
- Ninguna operación activa o modifica una `Conversation Release`.

## 3. Convenciones comunes

### 3.1. Identificadores

Todos los IDs internos son opacos y generados por la aplicación. Los comandos no aceptan IDs arbitrarios suministrados por el modelo para cambiar tenant, alcance o revisor.

```text
tenant_id
scope_id
reviewer_id
reviewer_binding_id
batch_id
item_id
decision_id
feedback_id
interpretation_id
candidate_change_id
delivery_attempt_id
command_id
```

### 3.2. Tiempo

- Timestamps: UTC, RFC 3339 con zona `Z`.
- Ventanas: semiabiertas `[window_start, window_end)`.
- El servicio aporta el tiempo autoritativo; el caller no elige `created_at`, `updated_at` ni tiempos de efectos.

### 3.3. Envelope de comando

Toda mutación usa:

```json
{
  "command_id": "cmd_opaque",
  "command_type": "record_review_decision",
  "actor": {
    "actor_type": "reviewer",
    "actor_id": "reviewer_opaque"
  },
  "aggregate": {
    "tenant_id": "tenant_opaque",
    "scope_id": "scope_opaque",
    "batch_id": "batch_opaque",
    "item_id": "item_opaque"
  },
  "expected": {
    "batch_revision": 3,
    "item_revision": 1,
    "session_fence": 4,
    "session_owner": "session_opaque"
  },
  "payload": {},
  "payload_fingerprint": "sha256:<lowercase-hex>"
}
```

Reglas:

1. `command_id` es único globalmente dentro del tenant.
2. El fingerprint se calcula sobre una serialización canónica versionada que incluye `command_type`, actor, aggregate, expected y payload.
3. Dentro de la transacción, el servicio busca primero `command_id`. Si existe con
   el mismo fingerprint, no vuelve a validar revisiones o fences actuales ni ejecuta
   la transición: devuelve el cuerpo durable original bajo `result` y marca el
   envelope de transporte como `replayed`.
4. Mismo `command_id` con otro fingerprint devuelve `idempotency_conflict` y no muta nada.
5. Una revisión/fence obsoleta devuelve conflicto; no se interpreta como replay.
6. Las respuestas del modelo nunca se consideran comandos hasta que la aplicación las valida y las transforma a este envelope.

`expected` es un objeto cerrado pero específico por comando:

- `create_review_batch`: `{}`; todavía no existe batch ni sesión;
- `claim_review_session`: exige `batch_revision`, pero no owner/fence previos para
  el primer claim; para reclaim exige además la revisión observada del lease;
- comandos interactivos: exigen `batch_revision`, `item_revision`,
  `session_owner` y `session_fence`;
- comandos de worker: exigen las revisiones del aggregate más `worker_owner` y
  `worker_lease_generation`; reciben la autoridad de sesión como snapshot durable
  del job, no impersonan al revisor.

Un campo no aplicable se omite; no se envía `null`. El schema exacto de cada
`command_type` rechaza campos faltantes y campos extra.

### 3.4. Envelope de resultado

```json
{
  "command_id": "cmd_opaque",
  "status": "applied",
  "aggregate_revision": 4,
  "result": {},
  "reason_code": null
}
```

`status` es uno de:

- `applied`: transición nueva confirmada;
- `replayed`: resultado durable existente;
- `conflict`: estado, fence o fingerprint incompatible;
- `rejected`: comando válido pero no autorizado o no permitido;
- `failed`: error operacional retryable antes de confirmar una transición.

En un replay sólo cambia `status` de `applied` a `replayed`; `command_id`,
`aggregate_revision`, `result` y `reason_code` reproducen los valores confirmados
originalmente. Si no existe el comando, recién entonces se validan principal
autenticado, owner, binding, revisiones, lease y fence. Un timeout sin resultado
recibido es ambiguo para el caller: debe reintentar el mismo comando, no generar otro.

## 4. Registros durables mínimos

Los siguientes son agregados conceptuales, no tablas definitivas.

### 4.1. `review_batch`

Campos mínimos:

```text
batch_id
logical_batch_key
tenant_id
scope_id
window_start
window_end
selection_contract_version
selection_config_fingerprint
reviewer_id
reviewer_binding_id
status
revision
active_session_owner
active_session_fence
session_lease_expires_at
created_at
closed_at
close_reason
```

`logical_batch_key` deriva de tenant, alcance, ventana y versión/configuración exacta de selección. Debe tener unicidad física.

Estados:

```text
pending
ready
in_review
completed
completed_empty
partially_completed
blocked
expired
```

### 4.2. `review_item`

Campos mínimos:

```text
item_id
batch_id
canonical_conversation_ref
window_activity_start
window_activity_end
release_observed_id
release_observed_version
position
status
revision
presentation_snapshot_id
current_decision_id
created_at
```

Unicidad física mínima:

- `(batch_id, position)`;
- `(batch_id, canonical_conversation_ref, window_activity_start, window_activity_end)`.

Estados:

```text
pending
presented
awaiting_feedback
awaiting_confirmation
accepted
corrected
feedback_cancelled
skipped
```

### 4.3. `presentation_snapshot`

```text
presentation_snapshot_id
item_id
snapshot_schema_version
renderer_version
sanitizer_version
minimization_policy_version
source_message_refs
rendered_payload
rendered_payload_hash
created_at
deleted_at
deletion_reason
```

Para el corte se utilizan textos ficticios. Si se elimina `rendered_payload`, permanecen hash, versiones y tombstone.

### 4.4. `review_decision`

Registro append-only:

```text
decision_id
item_id
decision_type
supersedes_decision_id
actor_id
command_id
created_at
```

`decision_type`:

```text
correcta
correcta_con_feedback
omitir
feedback_cancelled
```

Sólo una decisión no supersedida puede ser vigente por ítem.

### 4.5. `owner_feedback`

```text
feedback_id
item_id
decision_id
verbatim_text
content_hash
actor_id
command_id
created_at
superseded_at
revision
```

El texto original es inmutable. Para el corte: texto UTF-8 no vacío, máximo 4000 caracteres después de normalizar saltos de línea; no se hace trim destructivo del contenido almacenado.

### 4.6. `feedback_interpretation`

```text
interpretation_id
feedback_id
proposal_text
proposal_hash
model_contract_version
model_configuration_fingerprint
status
confirmed_text
confirmed_hash
actor_id
created_at
resolved_at
revision
```

Estados:

```text
proposed
confirmed
corrected
cancelled
superseded
```

La interpretación confirmada o corregida es la única elegible para clasificación.

### 4.7. `feedback_classification`

```text
classification_id
interpretation_id
classification_contract_version
classifier_configuration_fingerprint
category
artifact_target
scope_suggestion
rationale
status
created_at
revision
```

Estados: `recorded` y `superseded`. Una clasificación supersedida no puede crear
ni sostener un candidato o referencia de incidente vigente.

Categorías cerradas:

```text
factual_correction
brand_voice
conversation_policy
qualification_policy
evaluation_case
operational_incident
unsafe_or_prohibited_change
needs_clarification
```

`operational_incident` y `unsafe_or_prohibited_change` no son elegibles para `candidate_change`.

### 4.8. `candidate_change`

```text
candidate_change_id
classification_id
tenant_id
resolved_scope_id
observed_release_id
observed_release_version
base_release_id
base_release_version
base_artifact_versions
artifact_target
change_intent
status
command_id
created_at
superseded_at
revision
```

Estados:

```text
proposed
needs_clarification
superseded
```

Unicidad: una clasificación confirmada vigente produce como máximo un candidato
vigente. En este corte el candidato termina en `proposed` o
`needs_clarification`; `confirmed` y `ready_for_draft` quedan fuera de alcance. Si
la base activa cambió antes de crearlo, devuelve `stale_release_base`.

### 4.9. `operational_incident_reference`

```text
incident_reference_id
semantic_incident_key
item_id
feedback_id
classification_id
external_incident_id
status
command_id
created_at
revision
```

Estados: `linked`, `resolved`, `superseded`. La clave semántica fija tenant,
alcance, conversación, clase de falla y evidencia de origen. Sólo puede enlazar un
incidente creado o reutilizado por un servicio operativo separado; este contrato
no crea tickets ni comparte sus estados. Un replay reutiliza la referencia y una
clasificación supersedida supersede también el vínculo.

### 4.10. `review_delivery_operation` y `review_delivery_attempt`

La operación semántica y sus tentativas son identidades distintas:

```text
delivery_operation_id
semantic_delivery_key
payload_hash
status
active_attempt_id
next_attempt_number
```

Una operación admite como máximo un attempt reclamable o en vuelo. La unicidad
vive en `semantic_delivery_key`; cada retry autorizado incrementa
`attempt_number` bajo lock de la operación. El marker/idempotency key remoto se
deriva de la operación semántica y permanece estable entre attempts.

```text
delivery_attempt_id
delivery_operation_id
attempt_number
worker_owner
worker_lease_generation
batch_id
item_id
presentation_snapshot_id
reviewer_binding_id
session_fence
payload_hash
phase
outcome
remote_reference
reconciliation_deadline
created_at
request_started_at
finalized_at
```

Fases:

```text
reserved
request_started
finalized
```

Resultados finales:

```text
accepted
rejected
delivery_unknown
cancelled_before_request
```

`semantic_delivery_key` fija tenant, binding autorizado, lote, ítem, snapshot y
clase de mensaje. Un mismo key no puede apuntar a otro payload hash. Un attempt
nuevo sólo puede crearse tras rechazo definitivo sin efecto o reconciliación que
pruebe inequívocamente no aplicación; nunca mientras exista `request_started` o
`delivery_unknown` sin resolver.

## 5. Invariantes globales

1. Un registro nunca cambia de tenant ni alcance.
2. Un lote no mezcla revisores ni bindings.
3. Sólo el principal autenticado correspondiente a reviewer + binding +
   `session_owner`, con lease/fence vigente, puede avanzar una revisión.
4. Un ítem terminal no se sobrescribe; se enmienda mediante registros append-only.
5. Un feedback literal no se edita.
6. Una interpretación no confirmada no puede clasificarse como cambio aplicable.
7. Una clasificación de incidente o cambio prohibido no puede crear candidato.
8. Un candidato nunca valida, aprueba ni activa una release.
9. Un candidato conserva release observada, base elegida y artefactos base exactos.
10. Un mensaje sólo puede presentarse si su snapshot minimizado está persistido antes de reservar la entrega.
11. No puede existir `request_started` sin una autorización final exitosa en la misma transición durable.
12. Después de `request_started`, un timeout se registra como `delivery_unknown`; no se repite el POST a ciegas.
13. Los contadores de cierre se derivan de decisiones vigentes e ítems, nunca de incrementos independientes.
14. Toda clave idempotente rechaza payloads conflictivos.
15. Ninguna entrada de prospecto se ejecuta como instrucción del Copilot.

## 6. Comandos del primer corte

### 6.0. `claim_delivery_work`

**Actor:** worker autenticado mediante identidad de servicio.

El productor crea un job durable que captura batch, item, snapshot, binding,
`session_owner` y `session_fence` autorizados. El worker no impersona al revisor:
reclama ese job por lease propio y la aplicación compara el snapshot con la sesión
vigente al reservar y nuevamente antes de `request_started`.

Payload:

```json
{
  "delivery_job_id": "job_opaque",
  "worker_owner": "worker_opaque",
  "lease_seconds": 60
}
```

Resultado:

```json
{
  "delivery_job_id": "job_opaque",
  "worker_owner": "worker_opaque",
  "worker_lease_generation": 3,
  "worker_lease_expires_at": "2026-08-12T20:01:00Z",
  "session_authority_snapshot": {
    "session_owner": "session_opaque",
    "session_fence": 5
  }
}
```

Sólo la identidad de servicio dueña del claim puede renovar o usar esa generación.
Un lease vencido puede reclamarse con generación mayor; el worker viejo no reserva,
marca request-start ni usa la finalización normal. Si observó una respuesta externa
después de perder el lease, sólo puede entregar esa observación al inbox durable de
reconciliación; no puede mutar el attempt.

### 6.1. `create_review_batch`

**Actor:** operador.

Payload:

```json
{
  "tenant_id": "tenant_opaque",
  "scope_id": "scope_opaque",
  "window_start": "2026-08-12T00:00:00Z",
  "window_end": "2026-08-13T00:00:00Z",
  "selection_contract_version": "fixture-selection-v1",
  "selection_config_fingerprint": "sha256:...",
  "reviewer_id": "reviewer_opaque",
  "reviewer_binding_id": "binding_opaque",
  "fixture_set_id": "fixtures-v1"
}
```

Precondiciones:

- ventana válida y no vacía;
- tenant, scope, reviewer y binding compatibles;
- fixture set registrado y sanitizado;
- binding activo;
- corte ejecutado en modo `fixtures_only`.

Resultado:

```json
{
  "batch_id": "batch_opaque",
  "status": "ready",
  "item_count": 3,
  "batch_revision": 1
}
```

Si no hay ítems: `completed_empty`. Replay lógico con otro `command_id` reutiliza el mismo lote si todos los inputs durables coinciden; cualquier diferencia bajo la misma `logical_batch_key` devuelve `logical_batch_conflict`.

### 6.2. `claim_review_session`

**Actor:** revisor o adapter autenticado en su nombre.

Payload:

```json
{
  "batch_id": "batch_opaque",
  "reviewer_binding_id": "binding_opaque",
  "session_owner": "session_opaque",
  "lease_seconds": 120
}
```

Resultado:

```json
{
  "batch_id": "batch_opaque",
  "batch_status": "ready",
  "session_fence": 5,
  "lease_expires_at": "2026-08-12T20:02:00Z"
}
```

Reglas:

- el principal autenticado debe resolver exactamente al reviewer y binding del lote;
- `session_owner` es generado o ligado al contexto autenticado, no caller-selected;
- lease acotado por configuración del servidor;
- takeover sólo después de expiración o liberación explícita;
- cada claim exitoso incrementa el fence;
- claim no cambia `ready` a `in_review`; esa transición ocurre al proyectar la
  primera entrega aceptada;
- cada comando posterior exige el mismo principal, reviewer, binding,
  `session_owner` y fence;
- un dueño/fence viejo no puede registrar decisiones ni enviar presentaciones.

`claim_review_session` acepta lotes `ready`, `in_review` y
`partially_completed`. Al reclamar uno `partially_completed`, vuelve a
`in_review` si ya existe al menos un ítem presentado; no altera decisiones ni
ítems.

### 6.3. `get_next_review_item`

**Lectura autorizada**, no muta decisión.

Inputs:

```json
{
  "batch_id": "batch_opaque",
  "reviewer_binding_id": "binding_opaque",
  "session_fence": 5
}
```

Selección:

1. ítem no terminal de menor `position`;
2. si está `presented`, `awaiting_feedback` o `awaiting_confirmation`, devuelve ese mismo ítem;
3. nunca avanza por un evento tardío sin correlación explícita.

Resultado minimizado:

```json
{
  "item_id": "item_opaque",
  "item_revision": 1,
  "position": 1,
  "total": 3,
  "status": "pending",
  "presentation_snapshot": {
    "snapshot_id": "snapshot_opaque",
    "payload": {
      "context_summary": "Caso ficticio sanitizado",
      "apparent_objective": "Responder una consulta directa",
      "excerpts": [],
      "observed_outcome": "Sin respuesta posterior"
    },
    "payload_hash": "sha256:...",
    "release_observed": {
      "release_id": "release_fixture_1",
      "version": 1
    }
  }
}
```

### 6.4. `reserve_review_delivery`

**Actor:** worker determinístico.

Payload:

```json
{
  "batch_id": "batch_opaque",
  "item_id": "item_opaque",
  "presentation_snapshot_id": "snapshot_opaque",
  "reviewer_binding_id": "binding_opaque",
  "session_fence": 5,
  "session_owner": "session_opaque",
  "worker_owner": "worker_opaque",
  "worker_lease_generation": 3,
  "message_kind": "review_item",
  "payload_hash": "sha256:..."
}
```

Dentro de una transacción debe:

- validar binding activo y revocable;
- validar tenant/scope/batch/item/snapshot;
- validar lease/fence;
- validar que el snapshot de `session_owner`/fence aún sea vigente;
- validar identidad de servicio, `worker_owner` y generación de lease;
- asegurar estado presentable;
- crear o reutilizar attempt por `semantic_delivery_key`;
- dejarlo `reserved`.

No llama al conector.

### 6.5. `mark_review_delivery_request_started`

**Actor:** worker determinístico.

Revalida inmediatamente antes del POST:

- binding aún activo;
- mismo revisor/canal;
- mismo lote, ítem, snapshot y hash;
- sesión capturada todavía autorizada;
- worker owner/generación/lease vigentes;
- attempt aún `reserved`;
- modo del corte permite el conector simulado.

Transición atómica:

```text
reserved → request_started
```

Si se revocó la autoridad:

```text
reserved → finalized(cancelled_before_request)
```

No se permite POST si esta transición no confirmó `request_started`.

### 6.6. `finalize_review_delivery`

**Actor:** identidad de servicio dueña del worker lease/generación vigente.

Inputs:

```json
{
  "delivery_attempt_id": "attempt_opaque",
  "worker_owner": "worker_opaque",
  "worker_lease_generation": 3,
  "observed_result": "accepted",
  "remote_reference": "simulated-message-1"
}
```

Reglas de finalización normal:

- exige owner/generación/lease vigentes; un worker vencido recibe
  `worker_fence_stale` y no muta nada;

- `accepted`: proyecta durablemente la entrega sobre el ítem aunque el lease de
  sesión original haya vencido. Si el ítem sigue `pending`, pasa a `presented`; si
  ya avanzó de forma compatible, conserva el estado. Una aceptación incompatible
  bloquea el lote para reconciliación y nunca se descarta;
- `rejected`: finaliza sin presentar; el ítem permanece reintentable según política;
- timeout, respuesta no parseable o aceptación no demostrable: `delivery_unknown` con deadline;
- después de `request_started`, revocación o fence vencido no borran la evidencia externa;
- replay idéntico devuelve resultado previo; resultado final conflictivo falla cerrado.

La operación de entrega permanece única y no permite reservar otro attempt hasta
que aceptación y proyección estén confirmadas en la misma transacción o exista una
resolución explícita de no aplicación.

### 6.6.a. `submit_late_delivery_observation`

**Actor:** cualquier identidad de worker autenticada que demuestre haber poseído el
attempt exacto cuando se marcó `request_started`.

Esta operación append-only no finaliza ni proyecta. Inserta una observación con
unicidad sobre `(delivery_attempt_id, observation_fingerprint)` y conserva:

```text
delivery_attempt_id
original_worker_owner
original_worker_lease_generation
observed_result
remote_reference
observation_fingerprint
observed_at
submitted_at
```

La aplicación verifica que owner/generación coincidan con el attempt que ya está
`request_started`; rechaza attempts pre-request, payloads conflictivos y referencias
a otro efecto. El worker no necesita conservar el lease para aportar evidencia,
pero esa evidencia no tiene autoridad de finalización.

### 6.6.b. `claim_delivery_reconciliation`

**Actor:** identidad separada del reconciliador de servicio.

Reclama attempts `request_started` huérfanos o `delivery_unknown` mediante
`reconciliation_owner`, lease y generación propios. El reconciliador lee
observaciones append-only y/o consulta el conector por marker. Sólo el owner con
generación vigente puede ejecutar `reconcile_review_delivery`. Ésta revalida bajo
lock attempt, operación semántica, payload hash y resultado durable antes de
persistir aceptación, rechazo demostrado, unknown o bloqueo. Replay idéntico es
seguro; una evidencia final conflictiva falla cerrado.

### 6.7. `reconcile_review_delivery`

**Actor:** reconciliador autenticado con owner/generación/lease vigentes.

Para el conector simulado, consulta por `semantic_delivery_key` y contrasta las
observaciones tardías:

- marker inequívoco encontrado y payload hash coincide → `accepted`;
- prueba inequívoca de no aplicación antes del deadline → cierra el attempt y
  habilita, bajo lock de la operación, un nuevo `attempt_number` para la misma
  operación semántica;
- todavía ambiguo antes del deadline → permanece `delivery_unknown`;
- ambiguo al vencer → lote `blocked`, sin reenvío automático.

### 6.8. `record_review_decision`

**Actor:** revisor.

Payload:

```json
{
  "item_id": "item_opaque",
  "decision": "correcta_con_feedback",
  "supersedes_decision_id": null
}
```

Transiciones:

| Estado vigente | Decisión | Nuevo estado |
|---|---|---|
| `presented` | `correcta` | `accepted` |
| `presented` | `correcta_con_feedback` | `awaiting_feedback` |
| `presented` | `omitir` | `skipped` |
| terminal | cualquiera | sólo mediante enmienda con `supersedes_decision_id` exacto |

Una enmienda invalida interpretaciones, clasificaciones y candidatos derivados de la decisión supersedida dentro de la misma transacción o falla completa.

La operación de enmienda bloquea ítem, decisión vigente, feedback vigente,
interpretación vigente, clasificación, candidato y referencia de incidente. Exige
los IDs y campos `revision` exactos de todos los derivados presentes en un payload
cerrado `supersession_expected`. Un derivado ausente se declara explícitamente con
su ID omitido y `expected_absent=true`; la transacción verifica que no haya
aparecido. En una sola
transacción:

- inserta la nueva decisión con `supersedes_decision_id`;
- marca `owner_feedback.superseded_at`;
- cambia interpretaciones y clasificaciones a `superseded`;
- cambia candidatos y referencias de incidente a `superseded`;
- actualiza `current_decision_id` y el estado del ítem.

Si apareció o cambió cualquier derivado, devuelve
`decision_supersession_conflict` sin cambios parciales.

Ejemplo parcial del CAS:

```json
{
  "supersession_expected": {
    "decision_id": "decision_opaque",
    "feedback": {"id": "feedback_opaque", "revision": 1},
    "interpretation": {"id": "interpretation_opaque", "revision": 2},
    "classification": {"expected_absent": true},
    "candidate": {"expected_absent": true},
    "incident_reference": {"expected_absent": true}
  }
}
```

### 6.9. `record_owner_feedback`

Precondiciones:

- decisión vigente `correcta_con_feedback`;
- ítem `awaiting_feedback`;
- actor y fence vigentes;
- texto dentro de límites.

Transición:

```text
awaiting_feedback → awaiting_confirmation
```

Persiste feedback literal antes de invocar cualquier modelo. El modelo recibe una copia minimizada y el snapshot autorizado.

### 6.10. `propose_feedback_interpretation`

El Copilot devuelve únicamente:

```json
{
  "contract_version": "feedback-interpretation-v1",
  "feedback_id": "feedback_opaque",
  "interpretation": "Primero responder la duda directa y después retomar la calificación."
}
```

Validador determinístico:

- objeto JSON con claves exactas;
- versión exacta;
- `feedback_id` esperado;
- texto UTF-8 no vacío, máximo 2000 caracteres;
- sin instrucciones ejecutables ni referencias a otros tenants;
- un output inválido no cambia el estado y queda como fallo operacional retryable/acotado.

La persistencia crea una interpretación `proposed`; no crea candidato.

Persistir la propuesta es una mutación idempotente del servicio, con command
envelope, feedback revision y sesión vigentes. El JSON del modelo es data dentro
del payload validado, no el comando mismo.

### 6.11. `confirm_feedback_interpretation`

Payload:

```json
{
  "interpretation_id": "interpretation_opaque",
  "resolution": "confirm",
  "corrected_text": null
}
```

`resolution`:

- `confirm`: conserva propuesta como texto confirmado;
- `correct`: exige `corrected_text` válido y conserva ambos textos;
- `cancel`: exige `supersedes_decision_id`, `feedback_id` e interpretación vigentes
  exactos y ejecuta la operación atómica de enmienda a `feedback_cancelled`, sin
  clasificación ni candidato.

Sólo una resolución vigente por interpretación.

`confirm` y `correct` resuelven la interpretación y, en la misma transacción,
terminalizan el ítem como `corrected`. La clasificación posterior no condiciona el
juicio ya emitido; un fallo de clasificación queda retryable sin reabrir el ítem.

### 6.12. `classify_confirmed_feedback`

Salida del Copilot:

```json
{
  "contract_version": "feedback-classification-v1",
  "interpretation_id": "interpretation_opaque",
  "category": "conversation_policy",
  "artifact_target": "conversation_policy",
  "scope_suggestion": "offer_fixture_1",
  "rationale": "La corrección cambia el orden esperado de dos acciones."
}
```

La aplicación valida categorías y combinaciones:

| Categoría | Targets permitidos | Candidato permitido |
|---|---|---|
| `factual_correction` | `commercial_knowledge` | sí |
| `brand_voice` | `brand_voice`, `conversation_examples` | sí |
| `conversation_policy` | `conversation_policy` | sí |
| `qualification_policy` | `qualification_policy` | sí |
| `evaluation_case` | `evaluation_evidence` | sí |
| `operational_incident` | ninguno | no |
| `unsafe_or_prohibited_change` | ninguno | no |
| `needs_clarification` | ninguno | no |

Clasificación es propuesta estructurada, no autoridad para publicar.
`evaluation_evidence` queda fuera del manifiesto productivo de Conversation
Release: aporta casos de regresión a la validación, no se activa como artefacto
conversacional por sí solo.

### 6.13. `create_candidate_change`

Precondiciones:

- interpretación confirmada/corregida vigente;
- clasificación vigente y elegible;
- decisión y feedback no supersedidos;
- alcance resuelto por la aplicación;
- release base elegida explícitamente;
- release base todavía activa para ese alcance;
- artefactos base exactos disponibles.

Payload conceptual:

```json
{
  "classification_id": "classification_opaque",
  "resolved_scope_id": "offer_fixture_1",
  "base_release_id": "release_fixture_1",
  "base_release_version": 1,
  "base_artifact_versions": {
    "conversation_policy": 1
  },
  "change_intent": "Responder preguntas directas antes de retomar calificación."
}
```

Resultado inicial: `proposed` o `needs_clarification`. No puede devolver `validated`, `approved` ni `active`.

Este comando no crea `confirmed` ni `ready_for_draft`; esos estados y su autoridad
quedan para un contrato posterior de compilación de releases.

### 6.14. `link_operational_incident`

**Actor:** servicio operativo determinístico, no el Copilot.

Precondiciones:

- clasificación vigente `operational_incident`;
- decisión, feedback e interpretación no supersedidos;
- incidente externo ya creado o reutilizado por identidad semántica;
- tenant, alcance y conversación coincidentes.

Crea o reutiliza `operational_incident_reference`. Un replay no crea un segundo
ticket ni una segunda referencia. No cambia el estado conversacional del ítem y no
crea candidato.

### 6.15. `close_review_batch`

**Actor:** revisor u operador autorizado.

Modo:

```text
complete
pause
expire
block
```

Reglas:

- `complete` exige todos los ítems terminales y cero entregas ambiguas abiertas;
- `pause` produce `partially_completed` si existe al menos un ítem terminal y
  quedan pendientes; ese estado es reanudable mediante `claim_review_session`;
- lote sin ítems es `completed_empty` durante materialización, no mediante este comando;
- `block` exige reason code estable y no inventa decisiones;
- contadores se calculan transaccionalmente desde decisiones vigentes;
- `activated_changes` siempre es `0` y no se almacena como contador mutable.

Resultado:

```json
{
  "batch_id": "batch_opaque",
  "status": "completed",
  "counts": {
    "presented": 3,
    "accepted": 1,
    "corrected": 1,
    "feedback_cancelled": 0,
    "skipped": 1,
    "pending": 0,
    "incident_references": 0,
    "candidate_changes": 1,
    "activated_changes": 0
  }
}
```

## 7. Matriz canónica de comandos y estados

Esta matriz prevalece ante cualquier resumen ambiguo dentro de este documento.
Todos los comandos mutantes consultan primero replay por `command_id` y
fingerprint. Para comandos nuevos, validan principal, owner, binding, revisiones y
fence según corresponda.

| Comando | Estado de entrada | Estado de salida | Autoridad/fence |
|---|---|---|---|
| `create_review_batch` | inexistente | `ready` o `completed_empty` | operador; sin sesión |
| `claim_review_session` | `ready` | `ready` + nuevo lease/fence | principal = reviewer/binding/owner |
| `claim_review_session` | `in_review` | `in_review` + nuevo lease/fence | lease previo liberado/vencido |
| `claim_review_session` | `partially_completed` | `in_review` + nuevo lease/fence | principal = reviewer/binding/owner |
| `claim_delivery_work` | job pendiente o lease vencido | job leased + nueva worker generation | identidad de servicio/worker owner |
| `reserve_review_delivery` | ítem `pending` | operation/attempt `reserved` | sesión y worker fences vigentes |
| `mark_review_delivery_request_started` | attempt `reserved` | `request_started` o `cancelled_before_request` | autorización final atómica |
| `finalize_review_delivery(accepted)` | `request_started` | attempt `accepted`; ítem `presented`; primer ítem vuelve lote `in_review` | worker owner/generation/lease vigentes |
| `finalize_review_delivery(rejected)` | `request_started` | attempt `rejected`; ítem sin presentar | worker owner/generation/lease vigentes |
| `finalize_review_delivery(unknown)` | `request_started` | `delivery_unknown` | worker owner/generation/lease vigentes; no retry ciego |
| `submit_late_delivery_observation` | `request_started`/`delivery_unknown` | observación append-only; attempt sin cambio | worker autenticado + owner/generation históricos exactos |
| `claim_delivery_reconciliation` | huérfano `request_started` o `delivery_unknown` | reconciliation lease/generation | identidad separada de reconciliador |
| `reconcile_review_delivery(found)` | `request_started` huérfano/`delivery_unknown` | `accepted` + proyección de ítem | reconciliation owner/generation/lease vigentes |
| `reconcile_review_delivery(not_applied)` | `request_started` huérfano/`delivery_unknown` | attempt cerrado; operation retryable | reconciliation owner/generation/lease vigentes; nuevo attempt bajo lock |
| `record_review_decision(correcta)` | ítem `presented` | `accepted` | reviewer/binding/owner/fence |
| `record_review_decision(omitir)` | ítem `presented` | `skipped` | reviewer/binding/owner/fence |
| `record_review_decision(correcta_con_feedback)` | ítem `presented` | `awaiting_feedback` | reviewer/binding/owner/fence |
| `record_owner_feedback` | `awaiting_feedback` | `awaiting_confirmation` | reviewer/binding/owner/fence |
| `propose_feedback_interpretation` | ítem `awaiting_confirmation`, sin propuesta | interpretación `proposed` | servicio + sesión y feedback revision |
| `confirm_feedback_interpretation(confirm/correct)` | `awaiting_confirmation` | ítem `corrected`; interpretación `confirmed`/`corrected` | reviewer/binding/owner/fence |
| `confirm_feedback_interpretation(cancel)` | `awaiting_confirmation` | ítem `feedback_cancelled`; derivados supersedidos | CAS atómico de IDs/revisiones |
| `record_review_decision` con enmienda | ítem terminal | nuevo estado según decisión; anteriores supersedidos | CAS atómico de IDs/revisiones |
| `classify_confirmed_feedback` | interpretación `confirmed`/`corrected` | clasificación `recorded` | servicio + contrato de modelo validado |
| `create_candidate_change` | clasificación elegible `recorded` | `proposed` o `needs_clarification` | release base activa revalidada |
| `link_operational_incident` | clasificación `operational_incident` | referencia `linked` | servicio operativo, no modelo |
| `close_review_batch(complete)` | `in_review` | `completed` | todos terminales; cero unknown |
| `close_review_batch(pause)` | `in_review` | `partially_completed` | al menos uno terminal y pendientes |
| `close_review_batch(block)` | `ready`/`in_review` | `blocked` | reason code obligatorio |
| `close_review_batch(expire)` | `ready`/`in_review`/`partially_completed` | `expired` | política/tiempo del servidor; no inventa decisiones |

El snapshot se materializa durante `create_review_batch`; `get_next_review_item`
es una lectura pura y nunca crea o modifica snapshots.

## 8. Reason codes cerrados del corte

### Autorización

```text
reviewer_binding_missing
reviewer_binding_revoked
reviewer_mismatch
tenant_scope_mismatch
session_lease_missing
session_lease_expired
session_fence_stale
```

### Estado e idempotencia

```text
idempotency_conflict
logical_batch_conflict
aggregate_revision_conflict
invalid_state_transition
decision_supersession_conflict
derived_records_still_active
```

### Datos y modelo

```text
fixture_set_unknown
fixture_not_sanitized
presentation_snapshot_missing
presentation_hash_mismatch
invalid_interpretation_output
invalid_classification_output
classification_not_candidate_eligible
needs_clarification
stale_release_base
```

### Entrega

```text
delivery_already_finalized
delivery_result_conflict
delivery_rejected
delivery_unknown
reconciliation_pending
reconciliation_deadline_exceeded
```

### Seguridad

```text
real_data_not_allowed
unsafe_or_prohibited_change
cross_tenant_reference
untrusted_instruction_detected
```

Los errores desconocidos no se convierten en errores permanentes de dominio por aproximación. Permanecen retryable o bloqueados para inspección.

## 9. Auditoría mínima

Eventos append-only:

```text
review_batch.created
review_batch.materialized
review_session.claimed
review_session.reclaimed
review_item.delivery_reserved
review_item.delivery_request_started
review_item.delivery_finalized
review_item.delivery_reconciled
review_decision.recorded
review_decision.superseded
owner_feedback.recorded
feedback_interpretation.proposed
feedback_interpretation.resolved
feedback_classification.recorded
candidate_change.created
candidate_change.superseded
review_batch.closed
review_batch.blocked
```

Cada evento incluye IDs internos, actor, comando, revisiones/fence, transición y reason code. No incluye por defecto texto de conversaciones, feedback literal, secretos ni IDs externos sensibles.

## 10. API del Copilot en el corte

El Copilot sólo puede solicitar operaciones de negocio acotadas mediante un adapter que inyecta tenant, reviewer, binding, batch y fence desde la sesión autenticada.

Herramientas visibles:

```text
get_current_review
get_next_review_item
submit_review_decision
submit_owner_feedback
submit_interpretation_resolution
pause_or_complete_review
```

No son herramientas visibles del modelo:

```text
create_review_batch
reserve_review_delivery
mark_review_delivery_request_started
finalize_review_delivery
reconcile_review_delivery
create_candidate_change
activate_release
```

La clasificación y redacción de candidato son outputs estructurados solicitados por la aplicación, no autoridad para ejecutar mutaciones arbitrarias.

## 11. Matriz TDD del primer tracer bullet

La implementación debe avanzar test por test, observando RED antes de GREEN.

### Corte A — lote y selección

1. Crea un lote de tres fixtures y orden estable.
2. Replay exacto devuelve el mismo lote.
3. Misma clave con payload diferente falla `idempotency_conflict`.
4. Misma identidad lógica con inputs diferentes falla `logical_batch_conflict`.
5. Fixture no sanitizado falla `fixture_not_sanitized`.
6. Selección vacía produce `completed_empty`.

### Corte B — lease y presentación

1. Claim crea fence y lease.
2. Segundo owner antes del vencimiento es rechazado.
3. Reclaim posterior incrementa fence.
4. Fence viejo no puede avanzar.
5. Snapshot existe antes de reservar entrega.
6. Hash distinto falla cerrado.

### Corte C — efecto saliente simulado

1. Reserva no llama al conector.
2. Revocación antes de request-start produce `cancelled_before_request` y cero POST.
3. Request-start confirmado permite exactamente un POST.
4. Respuesta perdida después de aceptación produce `delivery_unknown`.
5. Reconciliación encuentra marker y finaliza accepted sin segundo POST.
6. Resultado final conflictivo es rechazado.
7. Ambigüedad vencida bloquea el lote.
8. Worker con lease o generación vencidos no puede ejecutar
   `finalize_review_delivery` y deja attempt e ítem sin cambios.
9. El owner histórico correcto puede ejecutar
   `submit_late_delivery_observation`, pero sólo agrega una observación: no
   finaliza el attempt, no proyecta el ítem y no habilita retry.
10. Owner/generación históricos incorrectos no pueden agregar observaciones.
11. Reconciliador sin lease o con generación obsoleta no puede ejecutar
    `reconcile_review_delivery` y deja attempt, operación e ítem sin cambios.
12. Reconciliador vigente consume una observación tardía compatible, finaliza y
    proyecta exactamente una vez; replay no agrega otra proyección.
13. Evidencias tardías conflictivas bloquean y nunca eligen una por recencia.

### Corte D — decisiones y feedback

1. `correcta` terminaliza como accepted.
2. `omitir` terminaliza como skipped.
3. `correcta_con_feedback` exige feedback.
4. Feedback literal persiste antes de invocar al Copilot.
5. Cancelar interpretación produce `feedback_cancelled` y cero candidato.
6. Enmienda supersede decisión y derivados atómicamente.
7. Replay tardío correlacionado con ítem anterior no avanza el siguiente.

### Corte E — interpretación y clasificación

1. Output con claves exactas se acepta.
2. ID incorrecto, categoría desconocida, texto vacío o claves extra falla cerrado.
3. Incidente no produce candidato.
4. Cambio prohibido no produce candidato.
5. Caso aislado apunta a evaluación, no regla global.
6. Configuración/modelo usados quedan fingerprinted.

### Corte F — candidato y cierre

1. Candidato conserva evidencia, release observada y base exacta.
2. Base obsoleta falla `stale_release_base`.
3. Misma clasificación produce un candidato vigente.
4. Lote no cierra con ítems pendientes.
5. Lote no cierra con delivery desconocida abierta.
6. Contadores se derivan correctamente tras una enmienda.
7. `activated_changes` es siempre cero.

## 12. Prueba de aceptación vertical

Fixture set:

1. conversación aceptada;
2. conversación corregida por orden de respuesta;
3. conversación omitida.

Secuencia:

1. crear lote;
2. reclamar sesión;
3. entregar ítem 1 y registrar `correcta`;
4. entregar ítem 2, registrar `correcta_con_feedback` y persistir texto;
5. proponer y corregir interpretación;
6. clasificar como `conversation_policy`;
7. crear candidato sobre release base exacta;
8. simular caída del cliente y reclaim con fence nuevo;
9. entregar ítem 3 con respuesta remota perdida;
10. reconciliar marker sin segundo POST;
11. registrar `omitir`;
12. cerrar lote;
13. reejecutar comandos relevantes y probar mismos resultados sin duplicados.

Aserciones finales:

```text
1 lote completed
3 ítems terminales
1 accepted
1 corrected
1 skipped
1 feedback literal inmutable
1 interpretación corregida y confirmada
1 clasificación conversation_policy
1 candidato proposed, nunca validated/approved/active
0 releases modificadas
0 incidentes
3 entregas aceptadas
1 POST total para la entrega reconciliada
0 efectos duplicados
activated_changes = 0
```

## 13. Condiciones para pasar de fixtures a datos reales

Todas son obligatorias y quedan fuera del primer tracer bullet:

- vinculación fuerte y revocable de identidad externa con revisor;
- canal y connector contract definidos;
- minimización verificable;
- cifrado en tránsito y reposo;
- controles de acceso por tenant;
- política y mecanismo de retención/eliminación;
- autorización final y reconciliación probadas contra el proveedor real;
- evidencia de que los links a Chatwoot respetan permisos;
- revisión de seguridad independiente;
- activación default-off y kill switch;
- E2E controlado con una única identidad autorizada.

## 14. Decisiones todavía abiertas

No bloquean implementar con fixtures:

- tecnología física de persistencia dentro de PostgreSQL;
- forma HTTP/RPC exacta;
- duración por defecto del lease;
- límites definitivos de textos;
- renderer y sanitizer productivos;
- proveedor/canal privado inicial;
- clasificación realizada en la misma sesión o por worker separado;
- política de retries y deadline real del conector;
- quién resuelve el alcance y release base antes del draft.

Sí debe mantenerse la semántica del contrato aunque cambien estos detalles.

## 15. Criterio de aceptación de este diseño

El diseño puede pasar a contrato implementable cuando se confirme que:

- las entidades y comandos cubren el primer recorrido completo;
- ninguna operación permite al Copilot saltar autorización o estados;
- idempotencia y fencing tienen identidad y conflicto explícitos;
- la entrega ambigua tiene reconciliación sin reenvío ciego;
- incidentes y cambios conversacionales permanecen separados;
- decisiones, feedback e interpretaciones pueden supersederse sin perder evidencia;
- un candidato queda cercado por release base y no adquiere autoridad de publicación;
- el tracer bullet puede implementarse test-first sin datos reales ni side effects productivos.
