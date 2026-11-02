# Contrato: el agente sabe que la persona es cliente (customer-status-v1)

Origen: E-297 (2026-11-02), P-11 del plan de la etapa 1, sobre el diseño de
E-278 (`productos/soporte-infoproductores/documentacion/2026-11-02-e278-diseno-el-agente-sabe-que-es-cliente.md`
en el vault) y las decisiones de Dan del 02/11 (D1 a D12, todas en su default
salvo D8, con un paso de SOP, y D10: prender en ATT1 lo antes posible).

## Por qué

Hasta acá el agente no sabía que hablaba con una clienta. `person_activity`
viaja solo si el manifiesto declara `[[productos]]` (ni ATT1 ni Maribel lo
hacen), busca solo por el teléfono del WhatsApp, mira 30 días y no ve
reembolsos. Lo medido: en ATT1 el equipo les mandó reactivaciones y links a mano
a tres clientas (E-037), y en Maribel las clientas son el 14,4 % de las
conversaciones con respuesta (E-092).

## La lectura de la base (migración `20261103000700`)

`get_customer_status_v1(p_tenant_ref, p_funnel_refs, p_chatwoot_account_id,
p_chatwoot_inbox_id, p_chatwoot_conversation_id, p_whatsapp_phone)`: solo
lectura, solo `service_role`, `security definer`. Una fila por funnel pedido en
el que la persona compró:

| Columna | Qué es |
|---|---|
| `funnel_ref` | el producto (un funnel por producto, 2c) |
| `status` | `cliente`; `reembolsada` si todas sus compras de ese producto tienen un `PURCHASE_REFUNDED`; `contracargo` si alguna tiene `PURCHASE_CHARGEBACK` o `PURCHASE_PROTEST` y no le queda ninguna vigente |
| `first_purchase_at`, `last_purchase_at` | la llegada de la primera y la última compra vigente (`webhook_events.received_at`) |
| `source` | la fuente más segura que la ata a la persona: `link`, `telefono` o `mail` |

**Qué compras cuenta.** Los `PURCHASE_APPROVED` de los funnels pedidos, por su
correlación portable (la admisión la escribe para toda compra del producto,
resuelta o no) o por una emisión de nuestro link en `purchase_matched` (que no
escribe la correlación portable, 20261016000300). **Sin ventana de tiempo:** ser
clienta no vence (D3).

**Las fuentes (D2), en orden:**

1. `link`: una emisión de **esta** conversación (cuenta, inbox y número de
   Chatwoot) que terminó en compra. Ata la compra a la conversación.
2. `telefono`: el `checkout_phone` (o `phone`) de la compra, o la identidad a la
   que se mandó el link, por las dos formas del móvil (52/521 y 54/549,
   `_whatsapp_phone_variants`).
3. `mail`: el mail de la compra contra los mails del contacto de este WhatsApp
   (`contact_points` de tipo `email` y `contacts.email`, sin mayúsculas ni
   espacios). El contacto sale de la identidad de WhatsApp o de un
   `contact_point` de teléfono.
4. La etiqueta `cliente` puesta a mano en la conversación **no** la ve la base:
   la suma el bridge (abajo).

**Reembolsos (D4).** Se leen por `data.purchase.transaction` en `webhook_events`
de Hotmart. Hoy el bridge descarta esos eventos (`unsupported_event_type`), así
que todo sale `cliente` hasta el PR que los acepta y los guarda; ese PR no
necesita otra migración, pero sí un fixture capturado de un reembolso real que
confirme la forma.

**Nada personal en la salida:** ni mail, ni teléfono, ni nombre, ni transacción,
ni payload. Entrada inválida: `22023` (`invalid_customer_status_input`). Correr
la migración dos veces: `55000` (`customer_status_already_applied`).

## Lo que hace el bridge (`AGENT_CUSTOMER_STATUS_ENABLED`, apagado por defecto)

Llega en el PR del bridge de E-297 (`bridge/customer_status.py`). Exige
manifiesto (Johanna no arranca con el flag) y `CHATWOOT_ACCOUNT_ID` e
`CHATWOOT_INBOX_ID`. Es portable: la regla es del producto y por instancia solo
se prende.

- **La entrada del agente** suma `customer_status` (`shadow-context-v5`), en toda
  aliada con manifiesto, declare `[[productos]]` o no (D1):

  ```json
  "customer_status": [{"product": "Alimenta tu Tiroides", "status": "cliente",
                       "since": "2026-10-19T15:00:00+00:00", "source": "telefono"}]
  ```

  `product` es la clave de `[[productos]]` o, sin productos, el `product_name` de
  `[hotmart]`. Sin compras no viene, que es lo mismo que hasta hoy.
- **La etiqueta `cliente`** de la conversación (`conversation.labels` del
  webhook; la misma que pone el paso `etiquetar: cliente` de un flujo nativo)
  marca como clienta del producto principal si la base no la vio, con
  `source: "etiqueta"` y sin `since`. Es la salida del equipo para quien compró
  con otros datos (D7, la #18 de ATT1).
- **Sin link de lo que ya compró (D6).** Si el modelo pide `send_payment_link`
  para un producto que está en `customer_status` (cualquier estado), el link no
  se reserva ni sale: el turno se deriva con el motivo
  `customer_link_request`. Lo hace el bridge, no el modelo.
- **Sin cupón (D4, D5).** Con `RECOVERY_COUPON_BY_REPLY_ENABLED`, a quien tiene
  algo en `customer_status` no se le pide la renovación por respuesta ni se le
  pone `recovery_coupon` en la entrada.
- **Si la lectura falla** (o la migración no está aplicada, 404), el agente
  contesta igual con lo que diga la etiqueta, y queda
  `customer_status_lookup_failed`.
- **El log** (`LOGGED_INFO_EVENTS`): `customer_status_context items=… statuses=…
  sources=… conversation=…`, `customer_status_link_blocked status=… source=…
  conversation=…` y `customer_status_coupon_skipped conversation=…`. Nunca el
  teléfono ni el mail.

## Lo que no hace (todavía)

- Lo que el agente **dice** a una clienta lo fija el SOUL común (ruta «Es
  cliente», D11 y D12): es el PR siguiente. Hasta entonces el agente recibe el
  dato sin una regla que lo use, y lo único que se garantiza es el freno del link
  y del cupón.
- Aceptar y guardar reembolsos y contracargos de Hotmart (D4).
- La carga del historial de Hotmart al instalar (D8): sin ella, una clienta de
  antes de la instalación entra como prospecta salvo por la etiqueta.
- La frase del motivo `customer_link_request` en la nota de la derivación: la
  nota muestra el código crudo («Motivo: customer_link_request.»), como cualquier
  motivo que `inbound_handoff_reason_sentence` no conoce.
