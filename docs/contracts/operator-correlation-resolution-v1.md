# Contrato de resolución manual de correlaciones V1

- **Estado:** Contrato aceptado
- **Versión:** `1.0.0`
- **Implementación:** local verificada; no mergeada ni desplegada
- **Complementa:** `operator-correlation-review-v1.md`

## Propósito

Aplicar una decisión humana explícita sobre una correlación determinística pendiente sin permitir que el modelo decida identidad, sin sobrescribir la evidencia original y sin autorizar efectos posteriores.

## Autoridades

```text
operador humano
→ aprueba la tool mutante en el gate nativo de Hermes

Client Copilot
→ explica, prepara y transporta la acción elegida
→ no fija tenant, funnel ni actor

bridge
→ autentica con bearer de escritura separado
→ fija tenant, funnel y actor desde configuración
→ valida formas y minimiza errores expuestos

PostgreSQL
→ revalida estado, scope, candidatos, concurrencia e idempotencia
→ registra comando y resolución
```

## Configuración default-off

Bridge:

```text
OPERATOR_CORRELATION_WRITE_ENABLED=false
OPERATOR_CORRELATION_WRITE_TOKEN=<bearer dedicado, mínimo 32 caracteres>
OPERATOR_CORRELATION_ACTOR_REF=<slug estable del operador, 2..64 caracteres>
```

El modo write requiere además la lectura de correlaciones habilitada, Supabase configurado y un write token distinto del read token.

Profile:

```text
OPERATOR_CORRELATION_WRITE_TOKEN=<mismo bearer dedicado>
```

El Profile no recibe tenant, funnel ni actor. El actor efectivo del request nunca
proviene de argumentos ni configuración del modelo: lo fija el bridge mediante
`OPERATOR_CORRELATION_ACTOR_REF`.

## Acciones y motivos cerrados

### `resolve_with_candidate`

Requiere `candidate_id` UUID y uno de:

- `external_transaction_reference`;
- `operator_source_record`;
- `customer_confirmation`.

Resultado aplicado:

```json
{
  "resolution_outcome": "linked_candidate",
  "effective_purchase_intent_id": "<candidate UUID>",
  "automation_blocked": true
}
```

### `close_without_match`

Requiere `candidate_id = null` y motivo:

- `no_valid_candidate_after_review`.

Resultado aplicado:

```json
{
  "resolution_outcome": "closed_without_match",
  "effective_purchase_intent_id": null,
  "automation_blocked": true
}
```

Mantener pendiente no llama ninguna tool mutante.

## HTTP interno

### Preparar

```http
POST /internal/operator/correlations/resolutions/prepare
Authorization: Bearer <write token>
Content-Type: application/json
```

Body:

```json
{
  "case_id": "uuid",
  "idempotency_key": "uuid",
  "action": "resolve_with_candidate | close_without_match",
  "candidate_id": "uuid | null",
  "verification_basis": "closed enum"
}
```

Respuesta `200`:

```json
{
  "command": {
    "command_id": "uuid",
    "idempotency_key": "uuid",
    "case_id": "uuid",
    "action": "...",
    "candidate_id": "uuid | null",
    "verification_basis": "...",
    "deterministic_outcome": "unmatched | ambiguous | conflict",
    "deterministic_reason_code": "...",
    "candidate_count": 0,
    "expires_at": "RFC3339",
    "requires_human_approval": true,
    "automation_blocked": true
  }
}
```

Preparar inserta únicamente un comando inmutable con vencimiento. No crea una resolución.
`idempotency_key` es obligatorio: el mismo key con el mismo request fingerprint devuelve
el mismo comando, incluso después de una respuesta perdida; el mismo key con otra
semántica falla `operator_correlation_idempotency_conflict`.

### Confirmar

```http
POST /internal/operator/correlations/resolutions/confirm
Authorization: Bearer <write token>
Content-Type: application/json
```

Body:

```json
{
  "command_id": "uuid",
  "expected_action": "resolve_with_candidate | close_without_match",
  "expected_candidate_id": "uuid | null"
}
```

La tool que llama este endpoint debe haber pasado el gate humano nativo de Hermes.

Respuesta `200`:

```json
{
  "resolution": {
    "resolution_id": "uuid",
    "command_id": "uuid",
    "case_id": "uuid",
    "resolution_outcome": "linked_candidate | closed_without_match",
    "effective_purchase_intent_id": "uuid | null",
    "deterministic_outcome": "unmatched | ambiguous | conflict",
    "applied_at": "RFC3339",
    "replayed": false,
    "automation_blocked": true
  }
}
```

Un replay exacto devuelve el mismo resultado con `replayed = true`.

## Validaciones de preparación

La transacción debe comprobar:

- scope y actor válidos;
- caso existente bajo tenant/funnel configurados;
- outcome original no inequívoco;
- `manual_handoff_required = true` y `purchase_intent_id = null`;
- ausencia de resolución aplicada;
- conjunto scoped completo: cantidad proyectada igual a `candidate_count` durable;
- cada candidato conserva tenant, funnel, producto, oferta y `waiting_for_purchase`;
- compatibilidad acción/candidato/motivo.

El snapshot almacena únicamente UUID, señales booleanas, lifecycle y `updated_at`; no contiene email, teléfono ni nombre.

## Validaciones de confirmación

Bajo el mismo lock del caso:

- comando existente, no vencido e inmutable;
- actor y scope iguales a los del comando;
- argumentos esperados iguales al comando;
- ausencia de una resolución distinta;
- outcome, reason, candidate count y bloqueo original sin cambios;
- snapshot scoped actual byte-equivalente al preparado;
- candidato elegido todavía presente y elegible.

## Errores públicos

| HTTP | `detail` | Semántica |
|---|---|---|
| `401` | `operator_write_authentication_required` | bearer ausente o incorrecto |
| `404` | `operator_correlation_case_not_found` | caso/comando fuera del scope o inexistente |
| `409` | `operator_correlation_already_resolved` | otra resolución ya ganó |
| `409` | `operator_correlation_command_expired` | venció la vista previa |
| `409` | `operator_correlation_stale_evidence` | cambió caso, scope o snapshot |
| `422` | `invalid_operator_correlation_resolution` | forma o combinación inválida |
| `503` | `operator_correlation_write_unavailable` | error no clasificable del backend |

No se exponen mensajes SQL, tokens, PII ni payloads.

## ACL e inmutabilidad

- RLS habilitada en tablas nuevas.
- Sin DML directo para `PUBLIC`, `anon`, `authenticated` o `service_role`.
- `service_role` recibe `EXECUTE` sólo sobre prepare/confirm.
- Trigger rechaza todo `UPDATE` y `DELETE` de comandos y resoluciones.
- Una restricción única garantiza una resolución por caso y una por comando.
- Una key UUID única más un fingerprint JSONB distingue replay exacto de conflicto.

## Efectos explícitamente ausentes

V1 no actualiza `purchase_intents`, correlaciones o candidatos; no autoriza activación; no crea acciones, reevaluaciones, deliveries ni mensajes. El caso deja de aparecer como pendiente únicamente porque la proyección read-only excluye resoluciones aplicadas.
