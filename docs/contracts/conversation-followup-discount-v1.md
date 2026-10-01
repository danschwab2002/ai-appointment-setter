# Contrato: seguimiento con cupon para quien contesto y no compro (v1)

- **Decision:** Dan, 2026-09-28 (vault: `productos/soporte-infoproductores/decisiones/2026-09-28-seguimiento-con-cupon-solo-a-quien-contesto.md`).
- **Codigo:** `src/bridge/followup_discount.py`, migracion `20260928000400_conversation_followup_discount_v1.sql`.
- **Gate:** `CONVERSATION_FOLLOWUP_ENABLED`, default `false`.

## Que hace

A quien nos escribio al menos una vez, recibio nuestra respuesta y se quedo callado 24 h sin comprar, le manda **una** plantilla aprobada de Meta con un 10 % de descuento y un boton que abre el checkout de Hotmart con el cupon ya cargado (`offDiscount`). Uno solo por conversacion, de por vida.

A quien nunca contesto no le manda nada. Plantillas de marketing a quien no interactua degradan la cuenta de Meta; esa es la regla que ordena todo lo demas.

## A quien (todo junto)

El bridge evalua contra Chatwoot, porque ahi viven esos hechos:

1. La conversacion esta `open` o `resolved`, sin `muted` ni `snoozed_until`. `pending` y `snoozed` se saltean con `conversation_status_excluded`. Una resuelta entra porque el equipo resuelve a mano para ordenar su bandeja, tambien lo que atendio solo el agente: resolver no dice nada del lead. La pausa, la derivacion y el opt-out si, y siguen siendo barreras (ver "Como barre").
2. No tiene la etiqueta `automation_opted_out` ni la etiqueta `automation_paused`. Pausada quiere decir en manos del equipo.
3. El contacto no esta bloqueado, tiene telefono E.164 y tiene nombre.
4. El lead escribio al menos un mensaje. Si no, se saltea con `never_replied`.
5. El ultimo mensaje conversacional es nuestro y es del AgentBot. Si es del lead, se saltea con `last_message_inbound`: el turno es del agente, y si nadie contesta le toca a la reactivacion. Si es de una persona del equipo, se saltea con `last_message_not_from_agent`.
6. Ninguna persona del equipo escribio despues del ultimo mensaje del lead.
7. Pasaron entre 24 h y 72 h desde el ultimo mensaje del lead. El techo evita escribirle al historico el dia que se prende el flag.

La base evalua al reservar (`claim_conversation_followup_v1`) lo que tiene que sobrevivir a un bridge mal configurado:

- no hay otro seguimiento vivo en la conversacion;
- no hay una derivacion sin atender (mismo predicado que la reactivacion desde `20260927000300`);
- no hay `human_takeover` ni estado pausado;
- el contacto no esta en `opted_out`, `blocked` ni `restricted`;
- no hay compra del lead en `purchase_intents` (`purchased`) ni en las identidades de `PURCHASE_APPROVED`/`PURCHASE_COMPLETE`. Se busca por mail o por los ultimos 9 digitos del telefono, porque Hotmart puede guardarlo sin codigo de pais.

Justo antes de reservar, el bridge relee la conversacion. Si el lead escribio en el medio, no se reserva nada (`lead_wrote_meanwhile`).

## Como barre

`ChatwootClient.list_recent_conversations_with_messages` pide `status=open` y despues `status=resolved`, cada uno con `sort_by=last_activity_at_desc`, y corta cada lista en la primera conversacion con `last_activity_at` anterior a `ahora - CONVERSATION_FOLLOWUP_MAX_AGE_SECONDS - 1 h`. De ahi para abajo todas son mas viejas, y ninguna puede tener un mensaje del lead dentro de la ventana: ningun mensaje es posterior a `last_activity_at`, y resolver la actualiza. Por cada conversacion arriba del corte trae el show y el historial.

- Falla cerrado (`conversation_scan_incomplete`) si `CONVERSATION_FOLLOWUP_MAX_PAGES`, que cuenta por estado, se agota antes del corte o del final de la lista.
- Tambien falla cerrado si un item no trae `last_activity_at` entero (`invalid_conversation_activity`) o si la lista no viene en el orden pedido (`conversation_order_unexpected`): sin eso el corte no es confiable. Un barrido incompleto que no falla se leeria como "no hay a quien".
- Una conversacion que aparece en los dos estados (el equipo la resolvio entre los dos listados) se lee una vez.
- Una conversacion que recibe actividad durante el barrido sube al principio de su lista y puede quedar afuera de esa pasada; la toma la siguiente, a los `CONVERSATION_FOLLOWUP_INTERVAL_SECONDS`.

Mandar la plantilla no reabre una conversacion resuelta: Chatwoot 4.13 solo reabre con un mensaje entrante (`Message#reopen_conversation`). Si el lead contesta, la conversacion se abre y el agente la atiende como cualquier respuesta.

Medido el 2026-10-01 en el inbox 9 (`docs/operations/2026-10-01-seguimiento-con-cupon-primer-envio.md`): las 149 conversaciones resueltas las resolvio a mano el equipo, y de las 11 que atendio solo el agente en 10 dias resolvio 8, 7 antes de las 72 h. Con el barrido de solo abiertas, desde la activacion el seguimiento salio una vez y perdio tres candidatas.

## Regimenes

Solo sirven para auditar. Todos reciben la misma plantilla.

| Regimen | Cuando |
|---|---|
| `link_sent_no_purchase` | Algun mensaje nuestro lleva un link `https://pay.hotmart.com/` |
| `went_quiet` | Pregunto, le contestamos y se callo, sin link |
| `payment_failed` | Recibio la plantilla de pago fallido. Se reconoce por el texto «no pudo completarse»: la API de mensajes de Chatwoot no devuelve `template_params` |

Diferidos por Dan: el lead que pidio descuento (hoy se deriva con `commercial_exception`) y el que dijo «mas adelante». Nunca reciben seguimiento: derivados o pausados, opt-out, y quienes ya compraron.

## El link

Lo emite `reserve_chatwoot_checkout_issuance_v2`, la misma RPC del agente, anclada en el ID de **nuestro** ultimo mensaje (`trigger_external_message_id`). Los IDs de Chatwoot son globales, asi que nunca coinciden con el de un mensaje del lead: el seguimiento siempre es una emision nueva, con su propio ULID. Lleva lo mismo que el link del agente:

```
https://pay.hotmart.com/<producto>?off=<oferta del lead>&checkoutMode=10&src=hermes&sck=<sck del anuncio>%7Chermes%7Cv1%7C<ULID>[&fbclid=<fbclid>]
```

- `checkout_url_final` conserva su contrato: la correlacion de la compra, la revision diaria y el recuperador lo leen sin cambios.
- El cupon **no** entra en esa URL. El bridge arma el boton con todo lo que va despues de `https://pay.hotmart.com/` mas `&offDiscount=<cupon>`.
- El codigo del cupon queda en `conversation_followup_events.coupon_code`.

## La plantilla

`parse_followup_template` la lee del catalogo del inbox en cada barrido y falla cerrado si no cumple todo esto:

- esta `APPROVED`;
- el idioma coincide con `CONVERSATION_FOLLOWUP_TEMPLATE_LANGUAGE` si esta declarada;
- el cuerpo tiene exactamente los marcadores `{{1}}` (primer nombre), `{{2}}` (producto) y `{{3}}` (cupon);
- tiene exactamente un boton, de tipo `URL`, con `url = https://pay.hotmart.com/{{1}}`.

El envio va por `processed_params.buttons = [{type: "url", parameter: <sufijo>}]`, que Chatwoot 4.13 traduce al componente del boton (`Whatsapp::TemplateProcessorService#process_button_components`). El primer nombre sale de la cadena de `bridge.lead_first_name`.

## El ciclo de un envio

Cada paso deja su registro:

1. `claim_conversation_followup_v1` aplica las barreras y emite el link. La fila del seguimiento queda `claimed` y la emision `reserved`.
2. `authorize_chatwoot_checkout_issuance_v2`, con el mismo ancla, pasa la emision a `request_started`.
3. El AgentBot manda la plantilla con la marca `content_attributes.conversation_followup_command_key` (`followup:<conversacion>:<nuestro ultimo mensaje>`).
4. Si sale bien: la emision queda `accepted_by_chatwoot` y el seguimiento `sent`.
5. Si Chatwoot falla o responde algo incierto: la emision queda `delivery_unknown` y el seguimiento `failed`. La siguiente reserva recibe `issuance_delivery_unknown` y no vuelve a mandar nada, porque mandar dos veces un cupon es peor que no mandarlo.

Un fallo **antes** de autorizar libera la fila, y el proximo barrido reusa el mismo link.

## Por que no se pisa con los otros motores

| Estado de la conversacion | Dueño |
|---|---|
| El lead escribio ultimo, dentro de la ventana | El agente (y el monitor de estancadas) |
| El lead escribio ultimo, mas de 24 h sin respuesta | La reactivacion |
| Escribimos nosotros y el lead se callo entre 24 y 72 h, sin compra | **Este seguimiento** |
| Derivada o pausada | El equipo: ningun motor automatico |

La reactivacion exige que el ultimo mensaje sea del lead y el seguimiento que sea nuestro, asi que no pueden elegir la misma conversacion en el mismo barrido. Los tests lo fijan en las dos direcciones sobre las conversaciones capturadas. Si el lead contesta el seguimiento y nadie le responde, la reactivacion tiene que poder actuar: por eso no hay una guarda que la bloquee.

## Configuracion

| Variable | Default | Nota |
|---|---|---|
| `CONVERSATION_FOLLOWUP_ENABLED` | `false` | Prendido sin lo siguiente, el bridge no arranca |
| `CONVERSATION_FOLLOWUP_TEMPLATE_NAME` | — | `johanna_seguimiento_descuento_01` |
| `CONVERSATION_FOLLOWUP_TEMPLATE_LANGUAGE` | — | `en` en Johanna: Meta registro asi `johanna_seguimiento_descuento_01` |
| `CONVERSATION_FOLLOWUP_COUPON_CODE` | — | `[A-Za-z0-9_-]{1,64}`, creado en Hotmart para el producto |
| `CONVERSATION_FOLLOWUP_PRODUCT_NAME` | — | el `{{2}}` de la plantilla |
| `CONVERSATION_FOLLOWUP_INTERVAL_SECONDS` | `300` | |
| `CONVERSATION_FOLLOWUP_MIN_AGE_SECONDS` | `86400` | 24 h |
| `CONVERSATION_FOLLOWUP_MAX_AGE_SECONDS` | `259200` | 72 h |
| `CONVERSATION_FOLLOWUP_MAX_SENDS_PER_SCAN` | `10` | |
| `CONVERSATION_FOLLOWUP_MAX_PAGES` | `5` | Por estado (`open`, `resolved`), 25 conversaciones por pagina |

Ademas exige el AgentBot, los IDs canonicos de Chatwoot, la admision de Corte B (el link sale del caso comercial que abre) y Supabase. No es una capacidad portable: su reserva depende hoy del catalogo de ofertas de Johanna, asi que prenderlo en un runtime de manifiesto frena el arranque.

`/ready` publica `conversation_followup` (estado del ultimo barrido) y `conversation_followup_last_scan` (solo conteos y motivos, sin datos de nadie). Un barredor caido no devuelve 503.

## Lo que queda afuera

- La revision diaria no clasifica todavia la marca `conversation_followup_command_key`: el mensaje aparece como plantilla generica.
- El fixture de la plantilla con boton es la forma documentada por Meta, no una captura. Meta aprobo la plantilla el 2026-09-28; falta capturarla del catalogo y reemplazar el fixture.
- Que hace WhatsApp con el sufijo del boton al tocarlo (si conserva `?`, `&` y `%7C`) no esta medido: el primer envio salio y se leyo, pero ningun evento de Hotmart trajo todavia su `sck`.
