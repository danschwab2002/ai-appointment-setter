# Contrato implementado — creación de lote diario con fixtures (Corte A)

- **Estado:** Implementado y verificado localmente
- **Versión:** `daily-owner-feedback-cut-a-v1`
- **Implementación:** `src/bridge/daily_feedback.py`
- **Pruebas:** `tests/test_daily_feedback.py`
- **Diseño rector:** [Contrato técnico propuesto del corte vertical](../design/client-copilot-feedback-vertical-slice-contract.md)
- **Alcance:** creación manual, idempotente y durable de un lote con snapshots sanitizados

## 1. Límite implementado

Este corte implementa solamente:

```text
fixture set sanitizado registrado en proceso
→ create_review_batch
→ lote immutable ready o completed_empty
→ ítems en orden estable
→ snapshot sanitizado por ítem
→ replay exacto y conflictos fail-closed
```

No implementa scheduler, conversaciones reales, canal del infoproductor, sesión de revisión, entregas, decisiones, feedback, interpretación, candidatos ni Conversation Releases.

## 2. Frontera HTTP controlada

La factory `create_daily_feedback_fixture_app(...)` crea una aplicación FastAPI separada para verificación y operación interna controlada. No está montada en `bridge.app` ni habilitada por defecto. Recibe un `FixtureOperatorGrant` server-side que liga el token exactamente a un tenant, scope, reviewer, binding activo y conjunto cerrado de fixtures.

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
```

- directorios: modo `0700`;
- registros: modo `0600`;
- creación de registros: temp file + `fsync` + hard link no-overwrite + `fsync` del directorio;
- lecturas: `O_NOFOLLOW`, owner efectivo y modo estricto;
- contenido preexistente distinto: `daily_feedback_storage_conflict`, nunca overwrite silencioso.

Cada operación publica primero un manifest de intención identificado por la clave lógica. El manifest fija el `command_id`, fingerprints, batch esperado y hash canónico de cada snapshot, batch e índice secundario. Después se materializan los artefactos y el commit record se publica al final con el hash del manifest. Bajo el lock, un retry sin commit reconcilia la intención original; inputs distintos fallan cerrado. Un replay con commit valida manifest, commit, presencia y hash de todos los artefactos, posiciones, snapshots y estado antes de devolver el batch. Evidencia faltante o alterada produce `daily_feedback_integrity_error`.

Los snapshots contienen únicamente el fixture sanitizado registrado, incluyendo referencia canónica ficticia, contexto, objetivo, resultado observado y release fixture. Este corte no admite conversaciones reales.

## 5. Estados producidos

- `ready`: uno o más ítems materializados;
- `completed_empty`: selección vacía;
- revisión inicial del lote: `1`.

Cada ítem conserva `position`, `fixture_id` y `snapshot_id`. El orden coincide exactamente con el orden del fixture set registrado.

## 6. Evidencia

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
