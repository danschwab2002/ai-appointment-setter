# Contrato: los toques 2 y 3 del carrito, A2 y Z (v1)

- **Estado:** propuesto, en construcción en la rama `feat/claude-att1-carrito-a2-z-v1`. Sin desplegar ni activar.
- **Fecha:** 2026-10-19
- **Alcance:** una instancia con el carrito portable y A1 con cupón ([hotmart-cart-abandonment-v1.md](hotmart-cart-abandonment-v1.md); `RECOVERY_DISCOUNT_FIRST_TOUCH_ENABLED`), y una política del carrito con tres pasos. Después de A1, el carrito recibe A2 a las 24 h y Z a las 72 h de A1.
- **Relacionados:** [portable-payment-slip-reminders-v1.md](portable-payment-slip-reminders-v1.md) (el primer flujo con pasos siguientes, del que este reusa el mecanismo), [approved-template-direct-dispatch-v1.md](approved-template-direct-dispatch-v1.md). La especificación es el flujo 3 y Z de «los flujos comerciales de ATT1 como tienen que quedar» (2026-10-17) y la decisión del 18/10 «el botón va directo al checkout y A2 es uno solo», fuera del repo.

## Propósito y límite

A1 sale a la hora del abandono con el 10 % y un cupón personal que dura 24 h. Quien no compra ni contesta recibe dos toques más:

- **A2**, el paso `cart_second_touch`: 24 h después de A1. En ATT1, `att1_carrito_dudas_01` (MARKETING), con nombre y producto, sin link y con dos respuestas rápidas: «Quiero mi 10 %» y «No más mensajes». Junta lo que más frena (el pago y las dudas del programa) y ofrece reactivar el 10 % a quien conteste.
- **Z**, el paso `cart_last_touch`: 72 h después de A1. En ATT1, `att1_ultima_renovacion_01` (MARKETING), con nombre, producto y vencimiento, el botón «Usar mi descuento» con nuestro link y el **cupón 2** (la única renovación del caso), y «No más mensajes».

**«Quiero mi 10 %» adelanta Z.** Si la persona toca ese botón en A2, Z sale en ese momento, con el cupón 2 y el link, en vez de esperar. Z es la renovación con el pago listo, así que:

- el agente no crea cupones (no tiene cómo, y su SOUL deriva los descuentos);
- no hace falta una «Z sin código» para quien ya usó la renovación: quien la pidió en A2 recibe Z una sola vez.

No están acá: el grupo sin mensaje, la regla de 30 días sin marketing después de Z, el aviso por la calidad del número y que el agente maneje la renovación en una conversación libre.

## Cómo entra cada paso

Los pasos son datos de la política del carrito de la instancia, después del primer contacto:

```json
[
  {"step_key": "first_contact", "mode": "freeform"},
  {"step_key": "cart_second_touch", "mode": "freeform", "delay": "24 hours", "approved_template": true},
  {"step_key": "cart_last_touch", "mode": "freeform", "delay": "72 hours", "approved_template": true}
]
```

con `max_automatic_messages = 3` y `expires_after = '5 days'`.

- Al aceptarse un toque, el motor inserta el paso siguiente como `no_reply_review` con `due_at` = la aceptación de A1 más el `delay` (absoluto desde el primer envío, `20260813000100`). Z cae a las 72 h de A1 aunque A2 se haya corrido por el horario: la distancia mínima entre A2 y Z puede bajar de 48 h, y no se compensa.
- El flujo de un paso siguiente sale de su `step_key` (`followup_steps.FOLLOWING_STEP_TRIGGERS`): los dos del carrito mapean a `cart_abandonment`, que es lo que el arranque del envío (`mark_lancemos_pilot_request_started`) autoriza con `PURCHASE_OUT_OF_SHOPPING_CART`.
- `approved_template: true` dice que el paso sale con una plantilla aprobada, fuera de la ventana de 24 h. Es lo que la base lee para no pausarlo por la ventana ni por una conversación resuelta, y para que no frene la adopción de una respuesta (ver la migración).

## El envío (bridge)

- **Configuración.** `RECOVERY_CART_FOLLOWUPS_ENABLED` (por defecto apagado) con `RECOVERY_CART_SECOND_TOUCH_TEMPLATE_NAME` y `RECOVERY_CART_LAST_TOUCH_TEMPLATE_NAME`. El idioma es el de las otras plantillas (`WABA_TEMPLATE_LANGUAGE`), y la categoría, MARKETING. Exige el modo directo; Z exige además `RECOVERY_COUPON_WORKER_ENABLED`. Un paso sin su plantilla cierra sin mensaje (`first_touch_template_not_configured`). Con el flag apagado, los dos pasos cierran como cerraba todo paso siguiente del modo directo (`approved_template_direct_unsupported_action`).
- **A2.** El catálogo del inbox se lee en cada envío y la plantilla tiene que cerrar con la categoría, las dos variables y solo respuestas rápidas.
- **Z.**
  - Lee el cupón 2 del caso (`get_recovery_coupon_for_case`, `ordinal = 2`). Si no está `valid`, el intento se difiere 8 minutos, hasta dos veces y dentro de los 30 minutos de su `due_at`; un cupón muerto (`create_failed`, `expired`, `redeemed`) o que no llegó a tiempo **cierra Z sin mensaje**. No hay plantilla de Z sin descuento, y Z no se manda sin el cupón que promete.
  - El link sale de `reserve_recovery_checkout_issuance` para la acción de Z, y el botón es ese link más `&offDiscount=<código>` (`followup_button_suffix`). Si el link no se puede emitir, Z cierra sin mensaje.
  - `vence` es el vencimiento real del cupón 2, en la zona del país del teléfono.
- Va a la conversación de A1 por `send_followup` con los `template_params` armados. El cerco final de Meta lo registra con el nombre de la plantilla del paso.

## «Quiero mi 10 %» (bridge y base)

- El webhook entrante, con el flag prendido y antes de la admisión, reconoce la respuesta exacta «Quiero mi 10 %» (sin mayúsculas, acentos, comillas ni espacios de más; con o sin espacio antes del %). Cualquier otro texto sigue el camino de siempre.
- Llama a `request_cart_renewal_v1` con la conversación, el mensaje y la persona:
  - `advanced`: había una Z pendiente de la persona en esa conversación. La base la marca como pedida y la pone a vencer ya. El agente no corre para ese mensaje.
  - `already_requested`: el mismo pedido otra vez (un reintento del webhook o un segundo toque del botón). El agente tampoco corre.
  - `no_pending_last_touch`: no hay Z pendiente. Sigue el camino de siempre: la admisión y el agente.
- Con la Z marcada como pedida, la reevaluación no la cancela por la respuesta (`prospect_replied`). Cualquier otra causa la corta igual: la compra, la baja o la derivación.
- El cupón 2 lo crea el worker de cupones cuando Z entra en su ventana, igual que el de A1. Hotmart lo aplica unos 5 minutos después de creado, así que una Z adelantada sale entre 5 y 20 minutos después del botón.

## La base (migración, después de este PR)

- `reevaluate_followup_action`: un `no_reply_review` cuyo paso de la política tiene `approved_template: true` no se pausa por la ventana de 24 h cerrada ni por una conversación resuelta. Z pedida por «Quiero mi 10 %» no se cancela por `prospect_replied`.
- `admit_portable_inbound_commercial_case_v1`: ese mismo paso pendiente no frena la adopción de la respuesta a A1 o a A2.
- `reserve_recovery_checkout_issuance_v1` acepta la acción del paso `cart_last_touch`.
- `claim_due_recovery_coupons_v1` siembra el cupón 2 para el paso `cart_last_touch`, con la misma anticipación que el 1.
- `plan_cart_recovery`: un carrito nuevo durante la secuencia no adelanta un `no_reply_review` pendiente (hoy hace `due_at = least(due_at, now())`).
- `request_cart_renewal_v1`, nueva, solo `service_role`.

## Qué lo corta

Lo mismo que A1: la compra de cualquier oferta del producto (20261019000300), la baja (incluido «No más mensajes»), la derivación, la pérdida del consentimiento y la respuesta de la persona después del toque anterior, salvo «Quiero mi 10 %». Cuenta en los topes del scope del carrito: cada envío es un arranque.
