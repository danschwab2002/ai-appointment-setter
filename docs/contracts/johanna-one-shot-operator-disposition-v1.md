# Contrato — disposición operativa de one-shot ambiguo V1

- **Estado:** Implementado y verificado localmente; no commiteado, migrado ni desplegado
- **Versión:** 1.0.0
- **Alcance:** cierre operativo auditable de un `delivery_unknown` precheckout cuyo contacto externo fue eliminado antes de reconciliar
- **No acredita:** aceptación por Chatwoot, envío, entrega física, ausencia del mensaje, ni autorización de retry

## 1. Problema y semántica

Un comando `delivery_unknown` conserva una verdad ambigua: el POST pudo o no haber
producido un efecto externo. Si el operador elimina el contacto de Chatwoot antes
de obtener el mensaje exacto, esa verdad ya no puede reconstruirse.

La disposición `unverifiable_external_contact_deleted` separa dos hechos:

1. el comando permanece `delivery_unknown / chatwoot_http_error`;
2. el operador confirma que el contacto de Chatwoot fue eliminado y que la
   reconciliación externa dejó de ser posible.

La disposición no muta el comando, no crea IDs de conversación o mensaje y no
permite un segundo POST. Una vez registrada, el trigger de commands bloquea
también las excepciones históricas de reconciliación y retry; no puede promoverse
posteriormente a `accepted_by_chatwoot` ni reabrirse como `request_started`.

## 2. Frontera durable

La tabla `johanna_one_shot_operator_dispositions` contiene exactamente una fila
por command:

- `command_id` con FK restrictiva al ledger one-shot;
- `disposition_code=unverifiable_external_contact_deleted`;
- `evidence_code=operator_confirmed_chatwoot_contact_deleted`;
- `operator_ref` sanitario;
- fingerprint semántico y timestamp server-side.

Toda actualización o eliminación falla con
`johanna_one_shot_operator_disposition_immutable`. RLS está activa y ningún rol
API, incluido `service_role`, recibe DML directo.

## 3. RPC

```text
resolve_johanna_one_shot_unverifiable_contact_deleted(uuid, text)
```

Entradas:

1. `p_command_id`: UUID interno exacto;
2. `p_operator_ref`: token `^[a-z0-9][a-z0-9:_-]{2,63}$`, nunca nombre libre,
   teléfono, email ni explicación narrativa.

La RPC es `SECURITY DEFINER`, fija `search_path` y sólo `service_role` puede
ejecutarla. Bloquea command y reevaluación, y sólo registra cuando:

- el command existe y sigue `delivery_unknown`;
- `failure_code` es exactamente `chatwoot_http_error`;
- no existen IDs de conversación ni mensaje;
- lleva al menos 24 horas finalizado;
- proviene de una reevaluación `precheckout_intent` completada como
  `command_reserved`;
- mantiene scope, generación, plantilla, copy, máximo de mensajes y ausencia de
  follow-ups exactos del first-touch productivo.

Cualquier divergencia devuelve un error categórico sin insertar una disposición.

## 4. Idempotencia

La primera llamada válida devuelve `recorded`. Un replay con el mismo command y
operador devuelve `replay` y la misma fila. El mismo command con otro operador o
fingerprint falla con `johanna_one_shot_operator_disposition_conflict`.

La RPC no llama Chatwoot, no toca el bridge, no reabre el command y no modifica
la intención, submission o reevaluación.

## 5. Readiness

`get_precheckout_delayed_first_touch_readiness()` mantiene su firma para
compatibilidad. `delivery_unknown_count` pasa a significar incógnitas **sin una
disposición válida**. El registro ambiguo continúa consultable en su ledger y la
disposición append-only conserva por qué dejó de bloquear el backlog operativo.

El tracking de esta migración se verifica en el ledger global y en el inventario
de esquema. El booleano histórico `migration_tracking_complete` conserva las seis
migraciones base del first-touch para no cambiar su contrato con réplicas antiguas.

## 6. Release y límites

Esta corrección no activa payment links, workers ni outbound. Durante su release:

```text
PAYMENT_LINK_ENABLED=false
```

debe permanecer sin cambios. Commit, merge, migración Cloud, llamada de
resolución, redeploy y nuevo E2E son autorizaciones separadas.
