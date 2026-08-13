# Contrato implementado — lote, presentación, retry y decisiones fixture-only (A–D1)

- **Estado:** Implementado y verificado localmente
- **Versión:** `daily-owner-feedback-decisions-d1-v1`
- **Implementación:** `src/bridge/daily_feedback.py`
- **Pruebas:** `tests/test_daily_feedback.py`
- **Diseño rector:** [Contrato técnico propuesto del corte vertical](../design/client-copilot-feedback-vertical-slice-contract.md)
- **Alcance:** lote durable, sesión fenced, entrega/retry simulados y decisiones reviewer append-only

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
→ request_started → finalized(delivery_unknown) con deadline
→ observación tardía append-only
→ claim exclusivo de reconciliación con lease/generación
→ found → accepted/presented/in_review sin segundo POST
→ unresolved vencido → batch=blocked
→ reserved → cancelled_before_request sin invocar el conector
→ request_started → conector simulado stateful, una invocación por attempt
→ finalized(rejected) sin presentar el ítem
→ delivery_unknown → observación not_applied inequívoca
→ sesión reviewer renovada + reconciler fenced + worker nuevo
→ attempt 1 finalized(not_applied) + attempt 2 reserved
→ item presented → correct / correct_with_feedback / skip
→ decisión append-only + feedback literal append-only cuando corresponde
→ siguiente ítem o batch completed
```

No implementa scheduler, conversaciones reales, canal productivo, POST externo,
interpretación, clasificación, candidatos, enmiendas ni Conversation Releases. El
feedback literal de D1 no activa aprendizaje ni muta producción. El conector de C2
es exclusivamente stateful y fixture-only dentro del store; no tiene endpoint HTTP
ni realiza red. El retry está acotado a un único successor (`attempt_number=2`) y no
habilita cadenas abiertas de reintentos.

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

### `POST /internal/daily-feedback/review-decisions`

Usa el mismo bearer reviewer ligado server-side. El reloj es server-owned y el
payload es cerrado:

```json
{
  "command_id": "opaque-command-id",
  "batch_id": "batch_opaque",
  "snapshot_id": "snapshot_opaque",
  "expected_item_revision": 2,
  "decision_type": "correct_with_feedback",
  "verbatim_feedback": "Responder la duda directa primero",
  "session_fence": 5
}
```

Combinaciones válidas:

| `decision_type` | `verbatim_feedback` | proyección |
|---|---|---|
| `correct` | `null` | `reviewed/revision 3` |
| `correct_with_feedback` | UTF-8 no vacío, hasta 4000 caracteres tras normalizar saltos sólo para medir | `reviewed/revision 3` |
| `skip` | `null` | `skipped/revision 3` |

El texto se almacena sin trim ni normalización destructiva en un artifact
`owner_feedback` separado, con ID y SHA-256 determinísticos. La decisión conserva el
vínculo vigente. Un comando nuevo sólo puede decidir el ítem no terminal de menor
posición; aunque una llamada interna hubiera presentado otro snapshot, D1 rechaza
avanzar fuera de orden. Si todos los ítems son terminales, el lote pasa atómicamente a
`completed/revision 3`; de lo contrario conserva `in_review/revision 2`. Los
contadores `reviewed_count`, `with_feedback_count` y `skipped_count` se derivan del
runtime y forman parte del resultado durable. Cada decisión recibe bajo lock una
`decision_sequence` contigua, incluida en su ID; el binding reconstruye con ella la
proyección histórica exacta del comando. Por eso un replay no puede devolver tipos,
estados, revisiones, feedback o contadores alterados aunque el lote haya avanzado.

Respuesta aplicada: HTTP `201`; replay exacto: HTTP `200`. Token inválido: `401`;
payload inválido: `422`; idempotencia, estado, revisión, owner, fence o lease stale:
`409`, sin filtrar el runtime.

Las fases de worker y la observación tardía no se exponen por HTTP. Se invocan como operaciones internas determinísticas del store.

### Reconciliación interna fixture-only

Un `FixtureReconciliationGrant` separado liga otro bearer token al `reconciliation_owner`; el token de reviewer no autoriza esta superficie. Los endpoints internos son:

- `POST /internal/daily-feedback/deliveries/reconciliation-claims` con `command_id`, `delivery_attempt_id` y lease de 1–300 segundos;
- `POST /internal/daily-feedback/deliveries/reconcile` con generación exacta, resolución `found`/`unresolved` y fingerprint de observación sólo cuando aplica.

El reloj es server-owned. Errores de token devuelven `401`; grant inactivo `403`; payload inválido `422`; idempotencia, lease/fence, evidencia o resultado conflictivo `409` sin filtrar el runtime.

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
<root>/review_decision_intents/
<root>/review_decisions/
<root>/owner_feedback/
<root>/review_decision_results/
<root>/review_decision_commits/
```

- directorios: modo `0700`;
- registros: modo `0600`;
- creación de registros: temp file + `fsync` + hard link no-overwrite + `fsync` del directorio;
- lecturas: `O_NOFOLLOW`, owner efectivo y modo estricto;
- contenido preexistente distinto: `daily_feedback_storage_conflict`, nunca overwrite silencioso.

Cada operación publica primero un manifest de intención identificado por la clave lógica. El manifest fija el `command_id`, fingerprints, batch esperado y hash canónico de cada snapshot, batch e índice secundario. Después se materializan los artefactos y el commit record se publica al final con el hash del manifest. Bajo el lock, un retry sin commit reconcilia la intención original; inputs distintos fallan cerrado. Un replay con commit valida manifest, commit, presencia y hash de todos los artefactos, posiciones, snapshots y estado antes de devolver el batch. Evidencia faltante o alterada produce `daily_feedback_integrity_error`.

Los snapshots contienen únicamente el fixture sanitizado registrado, incluyendo referencia canónica ficticia, contexto, objetivo, resultado observado y release fixture. Este corte no admite conversaciones reales.

Cada batch con sesión posee un único registro runtime reemplazado atómicamente bajo un `flock` global compartido también con creación, con archivo temporal, `fsync`, `os.replace` y `fsync` del directorio. Conserva status/revision actuales, lease/fence, ítems, attempts y resultados de comandos. Antes de cualquier lectura o mutación, el store valida exactamente `(fixture_id, position, snapshot_id)` contra el batch comprometido y cada snapshot debe declarar el fixture correspondiente.

Una decisión D1 usa un protocolo multiartifact bajo el mismo lock: publica primero
un intent write-once con fingerprints, hashes y las imágenes pre/post exactas del
runtime; publica decisión, feedback opcional y resultado como artifacts write-once;
reemplaza el runtime; y publica el commit al final. El binding compara el runtime
mutable contra estos artifacts externos y el commit. Un retry tras crash completa
sólo el intent original cuando el runtime coincide con su pre-state o post-state;
un estado tercero falla cerrado. Un replay committed valida artifacts y runtime
actual antes de reconstruir `runtime_commands`, sin retroceder un lote que avanzó.
Los inventarios de intents, resultados y commits deben tener stems idénticos; dentro
de cada batch, sus command IDs y decision IDs deben corresponder exactamente uno a
uno con las decisiones runtime. Un grafo huérfano, aunque sea autoconsistente, falla
cerrado.

Las proyecciones de ítem aceptadas son `pending/revision 1`, `presented/revision 2`,
`reviewed/revision 3` y `skipped/revision 3`. `ready/revision 1` exige cero ítems
involucrados; `in_review/revision 2` exige al menos uno presentado o decidido;
`completed/revision 3` exige que todos estén `reviewed` o `skipped` y ligados a una
decisión válida. `blocked/revision 2` exige cero involucrados y exactamente un attempt
`delivery_unknown` con evidencia durable de bloqueo posterior o igual al deadline.
El binding recalcula IDs/hashes y valida item → decisión → feedback; artifacts
huérfanos, vínculos rotos, secuencias no contiguas, resultados históricos o contenido
alterado fallan cerrado.

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

`delivery_unknown` exige referencia ambigua y deadline UTC futuro; mantiene item `pending` y batch `ready`, y bloquea otra finalización normal. `submit_late_delivery_observation` acepta `accepted` o `not_applied` del worker owner/generación históricos exactos, respaldados todavía por un grant server-side activo de la misma generación; el lease temporal sí puede estar vencido. Agrega evidencia sin finalizar ni proyectar.

`claim_delivery_reconciliation` usa identidad/grant separados, lease y generación monotónica. Cada claim nuevo aplicado —incluso del mismo owner durante su lease— incrementa la generación y cerca el claim anterior; otro owner sólo puede tomar control tras expiración. `reconcile_review_delivery(found)` exige una única referencia accepted compatible entre todas las observaciones y proyecta exactamente una vez. `unresolved` antes del deadline conserva unknown; al vencer persiste `blocked/revision 2`, que rechaza nuevos claims y reconciliaciones dentro de C1. Los replay exactos se resuelven antes de esa guardia terminal. Generación stale o evidencia conflictiva no mutan.

El Corte C2 añade `cancel_review_delivery_before_request`, válido sólo en `reserved`, con reason code cerrado y autoridad worker vigente. `invoke_simulated_delivery_connector` sólo acepta `request_started`, persiste un ledger determinístico con `invocation_count=1` y rechaza cualquier segundo comando para el mismo attempt. Si existe ledger, `finalize_review_delivery` exige que resultado y referencia coincidan exactamente; el binding runtime vuelve a comprobar esa relación en cada acceso, no sólo durante la finalización. `rejected` finaliza sin presentar ni avanzar el batch.

`reconcile_review_delivery_not_applied` exige: reconciler/grant/lease/generación vigentes; una única clase de evidencia `not_applied` sin evidencia `accepted`; referencia de reconciliación exacta emitida server-side por el ledger del conector al configurar el resultado posterior del unknown; sesión reviewer con `session_owner` distinto y fence estrictamente mayor al attempt 1; y worker con owner distinto y generación estrictamente mayor, respaldado server-side. Una autoridad meramente vigente pero reutilizada no basta. Una afirmación `not_applied` del worker sin prueba del conector tampoco habilita retry. La transición atómica cierra attempt 1 como `not_applied` y crea attempt 2 `reserved` con la misma semantic key, batch, snapshot, payload, reviewer y binding, pero nueva sesión y worker generation. Replay exacto reproduce attempt 2 y nunca crea attempt 3. El binding runtime recalcula IDs y valida predecessor, numeración, evidencia, ledger del conector y su coherencia con el resultado final del attempt.

## 6. Estados producidos

- `ready`: uno o más ítems materializados;
- `completed_empty`: selección vacía;
- `in_review`: primera aceptación proyectada;
- `completed`: todos los ítems tienen decisión terminal D1;
- revisión inicial del lote: `1`; primera proyección aceptada: `2`.
- `blocked`: ambigüedad no resuelta al vencer el deadline, sin presentar el ítem ni habilitar retry.

Cada ítem runtime empieza `pending/revision=1`; la aceptación lo lleva a
`presented/revision=2`; una decisión D1 lo lleva a `reviewed/revision=3` o
`skipped/revision=3`. El orden coincide exactamente con el fixture set registrado.

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
- finalización `delivery_unknown` con deadline y sin proyección;
- observación tardía append-only ligada al worker histórico;
- rechazo de observación tardía autoafirmada sin grant histórico activo;
- rechazo de contenido de observación adulterado mediante recálculo del fingerprint;
- claim, exclusividad, takeover y fencing del reconciliador;
- fencing inmediato ante un segundo claim aplicado del mismo owner;
- reconciliación found aceptada, replay exacto y conflicto entre referencias finales;
- unresolved antes del deadline y bloqueo durable al vencer;
- rechazo sin mutación de `found` posterior a un bloqueo;
- rechazo de runtime blocked sin attempt y de observación huérfana;
- claim/reconcile por HTTP real con token separado del reviewer.
- decisiones `correct`, `correct_with_feedback` y `skip`, avance uno por vez y cierre del lote;
- texto literal preservado, límite de 4000, artifact/hash separados y contadores derivados;
- replay antes de expiry/CAS, command ID conflictivo y rechazo de autoridad/revisión/reloj stale;
- tampering de decisión, puntero, feedback, hash y vínculo;
- reescritura coordinada del grafo runtime rechazada contra artifacts write-once;
- recuperación exacta tras crash en cada publicación D1 y rechazo de artifacts faltantes;
- decisión por HTTP real con bearer reviewer, payload cerrado y respuesta durable.
