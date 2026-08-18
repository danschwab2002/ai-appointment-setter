# Contrato first touch pre-checkout test-only V1

- **Estado:** Implementado en código; despliegue y E2E outbound pendientes
- **Versión:** 1
- **Interfaz:** `POST /internal/precheckout/test-first-touch`
- **No habilita:** abandono Hotmart, compra fallida, scheduler, dispatcher general ni follow-ups

## Propósito

Permitir un único mensaje WABA controlado para una intención pre-checkout ya persistida y para el único JID de prueba. La operación es manual, durable y at-most-once.

No clasifica la intención como abandono ni afirma que existió un error de pago.

## Activación

Todos los defaults permanecen apagados:

```text
PRECHECKOUT_FIRST_TOUCH_ENABLED=false
PRECHECKOUT_FIRST_TOUCH_TOKEN=
```

La activación exige además:

- receiver pre-checkout y test mode activos;
- JID canónico allowlisted;
- inbox WABA;
- Supabase y Chatwoot configurados;
- template `libre_ansiedad_test_first_touch_v1` aprobado en `es_AR`, categoría `MARKETING`.

## Request

Header:

```text
X-PRECHECKOUT-FIRST-TOUCH-TOKEN: <secreto separado>
```

Body exacto:

```json
{
  "command_key": "controlled-first-touch-001",
  "purchase_intent_id": "uuid"
}
```

El token se verifica antes de parsear el body. No se aceptan campos adicionales.

## Copy V1 aprobado

```text
¡Hola, {{1}}! Te habla el equipo de Johanna. Vimos que completaste el formulario de Libre de Ansiedad. ¿Te parece si avanzamos por acá?
```

`{{1}}` es exclusivamente el nombre persistido en la submission. La versión durable es `libre-ansiedad-precheckout-first-touch-v1`.

## Invariantes SQL

`begin_precheckout_test_first_touch` falla cerrado salvo que:

- la intención sea exactamente de Joana / Libre de Ansiedad / producto `F106691755G` / oferta `bxjge6zq`;
- continúe `waiting_for_purchase`;
- conserve `provisional=true`, `provider_observed=false`, `activation_authorized=false` y `whatsapp_contact_authorized=false`;
- el teléfono coincida exactamente con el target derivado del JID;
- exista una única identidad WhatsApp activa para ese teléfono;
- la identidad pertenezca exactamente al account e inbox WABA configurados;
- la conversación canónica proyecte el mismo ID externo de Chatwoot;
- el contacto no esté opted-out, bloqueado, restringido ni `do_not_contact`;
- exista una única conversación elegible sin takeover, pausa humana, bloqueo o cierre.

La command fija:

```text
test_only=true
generalizable=false
max_messages=1
followups_allowed=0
```

El scope durable `joana-libre-de-ansiedad-precheckout-test-v1` es único. Una
intención posterior para el mismo piloto no puede consumir un segundo mensaje,
aunque la intención original ya haya salido de `waiting_for_purchase`.

No crea `scheduled_actions`, `followup_sequences` ni `recovery_cases`.

## Semántica at-most-once

La RPC `begin_precheckout_test_first_touch` persiste `request_started` antes de cruzar Chatwoot.

- Primer request válido: `started`; puede cruzar Chatwoot una vez.
- Replay con `accepted_by_chatwoot`: HTTP 200, sin nuevo envío.
- Replay con `request_started`, `delivery_unknown`, `failed` o `cancelled`: HTTP 409 y reconciliación manual; nunca reenvía.
- Aceptación inequívoca de Chatwoot: `accepted_by_chatwoot` con IDs positivos.
- Error local previo al request: `failed`.
- Error HTTP o resultado ambiguo después de comenzar: `delivery_unknown`.

Si Chatwoot acepta y falla la finalización SQL, la respuesta exige reconciliación manual y el replay no puede reenviar.

## Respuestas HTTP

- `202`: primer request aceptado por Chatwoot.
- `200`: replay de una command ya aceptada.
- `400`: body o identificadores inválidos.
- `401`: token inválido.
- `409`: intención no elegible o reconciliación requerida.
- `413`: body mayor a 4 KiB.
- `502`: sender bloqueado o resultado no aceptado.
- `503`: feature, dependencias o finalización durable no disponibles.

## ACL

Las tablas no conceden acceso directo a `service_role`. Sólo `service_role` puede ejecutar:

- `begin_precheckout_test_first_touch`;
- `finish_precheckout_test_first_touch`.

Ambas RPC usan `SECURITY DEFINER` y `search_path = pg_catalog, public, pg_temp`.
