# Contrato implementado — lote y presentación diaria con fixtures (Cortes A–B)

- **Estado:** Implementado y verificado localmente
- **Versión:** `daily-owner-feedback-cut-b-v1`
- **Implementación:** `src/bridge/daily_feedback.py`
- **Pruebas:** `tests/test_daily_feedback.py`
- **Diseño rector:** [Contrato técnico propuesto del corte vertical](../design/client-copilot-feedback-vertical-slice-contract.md)
- **Alcance:** lote durable, sesión fenced, lectura del próximo ítem y presentación simulada aceptada

## 1. Límite implementado

Los cortes implementados cubren:

```text
fixture set sanitizado registrado en proceso
→ create_review_batch
→ lote immutable ready o completed_empty
→ ítems en orden estable
→ snapshot sanitizado por ítem
→ replay exacto y conflictos fail-closed
→ claim de sesión con lease y fence
→ lectura pura del primer ítem no terminal
→ reserve → request_started → finalized(accepted)
→ proyección atómica a item=presented y batch=in_review
```

No implementa scheduler, conversaciones reales, canal productivo, decisiones, feedback, interpretación, candidatos ni Conversation Releases. Tampoco implementa aún resultados `rejected`/`delivery_unknown`, conector externo, observaciones tardías ni reconciliación: pertenecen al Corte C.

## 2. Frontera HTTP controlada

La factory `create_daily_feedback_fixture_app(...)` crea una aplicación FastAPI separada para verificación y operación interna controlada. No está montada en `bridge.app` ni habilitada por defecto. Usa grants server-side distintos para operador y revisor; ningún payload crea autoridad.

### `POST /internal/daily-feedback/fixture-batches`

Autenticación:

```http
Authorization: Bearer <operator token>
```

El token se inyecta al crear la aplicación y nunca se persiste ni registra.

Payload cerrado:

```json
{
  "command_id": "opaque-command-id",
  "tenant_id": "tenant-opaque",
  "scope_id": "scope-opaque",
  "window_start": "2026-08-12T00:00:00Z",
  "window_end": "2026-08-13T00:00:00Z",
  "selection_contract_version": "fixture-selection-v1",
  "selection_config_fingerprint": "sha256:opaque",
  "reviewer_id": "reviewer-opaque",
  "reviewer_binding_id": "binding-opaque",
  "fixture_set_id": "fixtures-v1"
}
```

Las ventanas son UTC y semiabiertas `[window_start, window_end)`. Ambos límites deben ser timezone-aware, offset cero y `start < end`.

Respuesta nueva: HTTP `201`, `status=applied`.

Replay exacto: HTTP `200`, `status=replayed`, con el batch durable original.

Errores:

- `401 invalid_operator_token`;
- `403 operator_binding_inactive` si el grant está revocado;
- `403 operator_scope_denied` si tenant, scope, reviewer, binding o fixture set no coinciden exactamente con el grant;
- `409 idempotency_conflict` si un `command_id` se reutiliza con otro payload semántico;
- `409 logical_batch_conflict` si la misma clave lógica intenta usar otros inputs durables;
- `422` para payload, ventana o fixture set inválidos.

### `POST /internal/daily-feedback/review-sessions`

Un `FixtureReviewerGrant` liga el bearer token a `reviewer_id`, `reviewer_binding_id`, `session_owner` y estado activo. El payload sólo acepta `command_id`, `batch_id`, `expected_batch_revision` y `lease_seconds`. El reloj es server-owned y el lease permitido es de 1 a 300 segundos.

Un claim exitoso incrementa `session_fence`; no cambia `ready` a `in_review`. Otro owner sólo puede tomar control cuando `lease_expires_at <= now`. Replay exacto devuelve el resultado original y reutilización semántica incompatible falla `idempotency_conflict`.

### `GET /internal/daily-feedback/review-sessions/{batch_id}/next-item`

Exige el mismo grant, owner, fence y lease vigente. Es lectura pura: devuelve el ítem no terminal de menor posición y su snapshot sanitizado, hash y release fixture sin modificar el runtime.

Las fases de worker no se exponen por HTTP en este corte. Se invocan como operaciones internas determinísticas del store.

## 3. Clave lógica e idempotencia

La clave lógica deriva de:

```text
tenant_id
scope_id
window_start
window_end
selection_contract_version
selection_config_fingerprint
```

El fingerprint durable agrega reviewer, binding, identidad y contenido completo del fixture set sanitizado. Por eso modificar texto, release observada, orden o identidad de fixtures produce conflicto aunque los IDs aparentes coincidan.

Las operaciones de creación se serializan con un lock local. Esto garantiza la semántica sólo dentro de un filesystem compartido por el proceso/host. No es todavía una solución distribuida ni reemplaza la persistencia SQL prevista para producción.

## 4. Persistencia local

El store crea:

```text
<root>/commands/
<root>/logical/
<root>/batches/
<root>/snapshots/
<root>/manifests/
<root>/commits/
<root>/runtime/
<root>/runtime_commands/
```

- directorios: modo `0700`;
- registros: modo `0600`;
- creación de registros: temp file + `fsync` + hard link no-overwrite + `fsync` del directorio;
- lecturas: `O_NOFOLLOW`, owner efectivo y modo estricto;
- contenido preexistente distinto: `daily_feedback_storage_conflict`, nunca overwrite silencioso.

Cada operación publica primero un manifest de intención identificado por la clave lógica. El manifest fija el `command_id`, fingerprints, batch esperado y hash canónico de cada snapshot, batch e índice secundario. Después se materializan los artefactos y el commit record se publica al final con el hash del manifest. Bajo el lock, un retry sin commit reconcilia la intención original; inputs distintos fallan cerrado. Un replay con commit valida manifest, commit, presencia y hash de todos los artefactos, posiciones, snapshots y estado antes de devolver el batch. Evidencia faltante o alterada produce `daily_feedback_integrity_error`.

Los snapshots contienen únicamente el fixture sanitizado registrado, incluyendo referencia canónica ficticia, contexto, objetivo, resultado observado y release fixture. Este corte no admite conversaciones reales.

Cada batch con sesión posee un único registro runtime reemplazado atómicamente bajo un `flock` global compartido también con creación, con archivo temporal, `fsync`, `os.replace` y `fsync` del directorio. Conserva status/revision actuales, lease/fence, ítems, attempts y resultados de comandos. Antes de cualquier lectura o mutación, el store valida exactamente `(fixture_id, position, snapshot_id)` contra el batch comprometido y cada snapshot debe declarar el fixture correspondiente.

Las únicas proyecciones aceptadas en Corte B son `pending/revision 1` y `presented/revision 2`. `ready/revision 1` exige cero ítems presentados; `in_review/revision 2` exige al menos uno. Cualquier combinación individualmente plausible pero semánticamente contradictoria falla cerrado.

`commands/` y `runtime_commands/` forman un único namespace global de `command_id`. El primero conserva comandos de creación comprometidos; el segundo materializa `command_id → fingerprint + batch + resultado` para runtime. Si un crash ocurre después del reemplazo runtime y antes del índice, el próximo lookup reconcilia el resultado desde todos los runtimes bajo el mismo lock antes de evaluar semántica nueva. Replay se resuelve antes de CAS/revisión/fence; reutilización entre creación/runtime, otro batch o tipo de comando falla `idempotency_conflict`.

Esta unidad evita publicar parcialmente aceptación y proyección, pero continúa siendo persistencia local de un solo host, no un workflow distribuido.

## 5. Sesión y entrega simulada

`claim_review_session` valida la autoridad inmutable del batch y la revisión runtime bajo lock. Permite `ready` e `in_review`, replay exacto y takeover sólo tras expiración. Cada claim nuevo incrementa el fence.

`get_next_review_item` devuelve primero un ítem ya `presented` no terminal; por eso una reanudación no avanza silenciosamente al siguiente antes de registrar la futura decisión.

La entrega simulada implementada usa:

```text
reserve_review_delivery
→ reserved
→ mark_review_delivery_request_started
→ request_started
→ finalize_review_delivery(accepted)
→ finalized(accepted) + item presented + batch in_review
```

Reserva y request-start exigen reviewer/binding/session owner/fence vigentes y un `WorkerLeaseGrant` server-side activo cuyo owner, generación y vencimiento dominan exactamente al attempt. El worker no crea autoridad mediante el payload. Después de `request_started`, una aceptación demostrada puede proyectarse aunque el lease de sesión haya vencido, pero sigue exigiendo el grant worker capturado y vigente. Un reclaim con nueva generación cerca inmediatamente la generación anterior. Un payload hash distinto, segundo attempt para la misma clave semántica, fase incorrecta o autoridad stale falla cerrado. Cada comando persiste fingerprint y resultado durable para replay exacto.

## 6. Estados producidos

- `ready`: uno o más ítems materializados;
- `completed_empty`: selección vacía;
- `in_review`: primera aceptación proyectada;
- revisión inicial del lote: `1`; primera proyección aceptada: `2`.

Cada ítem runtime empieza `pending/revision=1` y la aceptación lo lleva a `presented/revision=2`. El orden coincide exactamente con el fixture set registrado.

## 7. Evidencia

`tests/test_daily_feedback.py` cubre:

- orden estable;
- replay tras reabrir el store;
- conflicto de command ID;
- conflicto de lote lógico;
- rechazo de fixtures no sanitizados;
- lote vacío;
- snapshots privados completos;
- ventanas inválidas o no UTC;
- fixture IDs duplicados;
- conversaciones canónicas duplicadas dentro del mismo lote;
- conflicto con artefacto preexistente;
- creación, replay y rechazo sin credencial mediante un servidor HTTP real.
- crash recovery después de cada punto de publicación durable;
- rechazo de replay ante snapshot faltante o batch alterado;
- autorización exacta por tenant, scope, reviewer, binding, fixture set y estado activo.
- replay exacto y conflicto semántico también para `command_id` secundarios asociados al lote.
- claim, replay, takeover, revocación, revisión stale y lease acotado;
- lectura pura y rechazo de owner/fence/lease stale;
- reserva, request-start y aceptación proyectada atómicamente;
- replay durable de las tres fases de entrega;
- rechazo de payload alterado, operación duplicada, fase inválida y worker stale;
- finalización aceptada tras vencer la sesión pero antes de vencer el worker;
- reclaim de un batch `in_review` mediante revisión runtime;
- claim y lectura de ítem mediante servidor HTTP real.
- replay tardío de claim después de avanzar batch revision;
- unicidad global de command ID entre batches y tipos de comando;
- unicidad global de command ID entre creación y runtime, en ambas direcciones;
- rechazo de swap, eliminación, duplicación, posición, status y revisión adulterados;
- rechazo de parejas item status/revision y proyección batch contradictorias;
- worker generation vieja cercada tras reclaim server-side;
- rechazo de reviewer grant con token vacío al construir la aplicación.
