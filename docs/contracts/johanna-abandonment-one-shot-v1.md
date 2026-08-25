# Contrato one-shot de abandono Johanna V1

- **Estado:** Implementado y verificado localmente; migración, despliegue y E2E físico pendientes
- **Versión:** 1
- **Interfaz:** `POST /internal/johanna/abandonment-one-shot`
- **Plantilla única:** `johanna_carrito_abandonado_01`, `es_EC`, `MARKETING`
- **No habilita:** dispatcher, scheduler, workers Hotmart, outbound general ni follow-ups

## Propósito

Ejecutar un único first-touch WABA supervisado para una intención real `lead.precheckout`
V1.1.0 autorizada. El sender busca o crea el contacto en Chatwoot, crea una conversación y
publica la plantilla aprobada; no requiere que el lead haya escrito previamente por WhatsApp.

Este corte valida la infraestructura física de first-touch. No afirma por sí solo que Hotmart
haya emitido `PURCHASE_OUT_OF_SHOPPING_CART` y no convierte el receiver del formulario en
autorización outbound general.

## Activación

Los defaults son:

```text
JOHANNA_ABANDONMENT_ONE_SHOT_ENABLED=false
JOHANNA_ABANDONMENT_ONE_SHOT_TOKEN=
```

La factory sólo acepta el gate activo cuando coinciden exactamente:

- receiver `lead.precheckout` activo;
- scope `johanna-abandonment-template-e2e`, versión `1`;
- tenant de despliegue `psicologajohanna`;
- provider `waba`;
- channel account `chatwoot-inbox:9`;
- Chatwoot account `1`, inbox `9`;
- plantilla `johanna_carrito_abandonado_01`;
- sin plantilla de follow-up;
- idioma `es_EC` y categoría `MARKETING`;
- JID individual canónico y token operador separado de al menos 32 caracteres.

El dispatcher y `DURABLE_OUTBOUND_ENABLED` permanecen apagados.

## Request

Header, validado antes de leer el body; el proceso falla al arrancar si el secreto
tiene menos de 32 caracteres:

```text
X-JOHANNA-ONE-SHOT-TOKEN: <secreto separado>
```

Body JSON exacto, máximo 8 KiB:

```json
{
  "command_key": "johanna-abandonment-real-e2e-001",
  "purchase_intent_id": "uuid"
}
```

No se aceptan campos adicionales.

## Reserva durable

`begin_johanna_abandonment_one_shot` bloquea el scope global, toma el mismo fence
cuenta+teléfono que el writer canónico de opt-out, bloquea cualquier contacto e
identidad canónica existentes y relee autoridad antes de insertar `request_started`.
Falla cerrado salvo que:

- el scope esté publicado y corresponda a WABA, account/inbox, producto Hotmart
  `8104005` y oferta `bxjge6zq`;
- el runtime esté `inactive`, generación `0`;
- la intención esté `waiting_for_purchase`, no provisional, observada y autorizada;
- el teléfono coincida exactamente con el JID allowlisted;
- exista evidencia V1.1.0 sin conflicto con `marketing_optin=true`,
  `whatsapp_contact=true` y `copy_version=johanna-precheckout-whatsapp-disclosure-v1`;
- producto de landing `F106691755G` y oferta `bxjge6zq` coincidan;
- no exista más de un owner interno para el teléfono;
- cualquier owner interno existente no esté opted-out, bloqueado, restringido ni
  `do_not_contact`;
- no exista evidencia durable de opt-out Chatwoot para la misma cuenta y teléfono,
  incluso cuando todavía no haya identidad WABA interna;
- el presupuesto singleton `johanna-abandonment-template-e2e-v1` siga vacío.

La command fija `max_messages=1` y `followups_allowed=0`. No crea casos, secuencias,
actions ni delivery attempts del dispatcher general.

## Efecto Chatwoot

Después de reservar:

1. `find_contact_by_phone` busca el teléfono exacto en inbox `9`;
2. si no existe, `create_contact` provisiona el contacto;
3. `create_conversation` crea la conversación en inbox `9`;
4. `send_first_message` publica la plantilla con `{{1}} = buyer_name` y
   `{{2}} = product_name`;
5. `finish_johanna_abandonment_one_shot` registra IDs positivos como
   `accepted_by_chatwoot`.

Una aceptación Chatwoot es evidencia de gateway/CRM, no de entrega física. La evidencia física
requiere observar el estado provider `sent|delivered|read` y confirmación del receptor.

## Replay y ambigüedad

- Primera reserva: `started`; puede cruzar Chatwoot una vez.
- Replay `accepted_by_chatwoot`: HTTP 200, cero nuevo POST.
- Replay `request_started` o `delivery_unknown`: HTTP 409; reconciliación manual.
- Error o respuesta ambigua del sender: se finaliza `delivery_unknown`; nunca se reenvía a ciegas.
- Si Chatwoot acepta y falla la finalización SQL, la command permanece
  `request_started`; todo replay exige reconciliación.

## Respuestas HTTP

- `202`: primera command aceptada por Chatwoot;
- `200`: replay ya aceptado;
- `400`: body o identificadores inválidos;
- `401`: token inválido;
- `409`: gate SQL rechazado, metadata divergente o reconciliación requerida;
- `413`: body mayor a 8 KiB;
- `502`: sender no produjo aceptación inequívoca;
- `503`: feature, dependencias o finalización durable no disponibles.

## ACL y privacidad

La tabla no concede acceso directo a roles API ni a `service_role`. Sólo `service_role`
puede ejecutar los RPC `begin_*` y `finish_*`; ambos usan `SECURITY DEFINER` y
`search_path = pg_catalog, public, pg_temp`.

No se registran payload, teléfono, email, token, firma ni contenido del mensaje. Las respuestas
exponen sólo status estable y el UUID interno de la command aceptada.
