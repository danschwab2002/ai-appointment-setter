# Contrato: el seguimiento del pago rechazado, B2 o Z (v1)

- **Estado:** propuesto, en construcción en la rama `feat/claude-att1-pago-rechazado-seguimiento-v1`. Sin desplegar ni activar.
- **Fecha:** 2026-10-19
- **Alcance:** una instancia con B1 en variantes ([portable-payment-failure-variants-v1.md](portable-payment-failure-variants-v1.md); `PAYMENT_FAILURE_VARIANTS_ENABLED`) y una política del pago rechazado con dos pasos. Después de B1, quien no contestó recibe un seguimiento 24 h después, y nada más.
- **Relacionados:** [portable-cart-followups-v1.md](portable-cart-followups-v1.md) (las marcas de los pasos siguientes y la Z que se reusa), [approved-template-direct-dispatch-v1.md](approved-template-direct-dispatch-v1.md). La decisión es de Dan del 19/10: el pago rechazado manda como máximo dos mensajes.

## Propósito y límite

B1 sale a los 15 minutos del último rechazo, con una de cuatro variantes. A quien no compra ni contesta le llega **un** seguimiento 24 h después de B1:

- **B2** (`att1_pago_rechazado_descuento_01`, MARKETING) si B1 salió **sin cupón**: dos rechazos, el banco, o la plantilla de hoy porque la variante no se pudo armar. Le deja un 10 % «como ayer el pago no se pudo completar».
- **Z** (`att1_ultima_renovacion_01`, MARKETING, la misma del carrito) si B1 salió **con cupón**: el de otro caso o el del saldo. Es la renovación por última vez.

Las dos van con nombre, producto y vencimiento (en ese orden), el botón «Usar mi descuento» con nuestro link y el **cupón 2** del caso (para B2 es el primer código de la persona; para Z, la renovación), y la respuesta rápida «No más mensajes». **Nunca sale una promesa de descuento sin el cupón `valid` detrás:** si el cupón no está listo a tiempo, murió, o el link no se puede emitir, el seguimiento cierra sin mensaje.

No hay tercer toque, ni «Quiero mi 10 %» en este flujo.

## La política

La publica la instancia, no este cambio. Para ATT1, `att1-pago-rechazado-dos-toques` v1:

```json
[
  {"step_key": "payment_failure_first_contact", "mode": "freeform"},
  {"step_key": "payment_failure_followup", "mode": "freeform", "delay": "24 hours",
   "approved_template": true, "coupon_ordinal": 2}
]
```

con `max_automatic_messages = 2`, `expires_after = '3 days'`, gracia de 15 minutos y ventana de 09:00 a 21:00 de Ciudad de México. `mode` es `freeform`, como en el carrito: la reevaluación no admite otro y el bridge reserva el intento como plantilla aprobada porque el scope es de WABA.

Al aceptarse B1, el motor inserta el seguimiento como `no_reply_review` con `due_at` = la aceptación más 24 h (`20260813000100`). Las marcas de `20261019000600` hacen el resto: la reevaluación no lo pausa por la ventana de 24 h cerrada ni por una conversación resuelta, la adopción de una respuesta no lo cuenta como pendiente, el worker del cupón siembra el cupón 2 con la anticipación y la vigencia de siempre, y la reserva del link lo acepta.

## La base (migración `20261019000700`)

- `payment_failure_first_touch_records` (nueva): una fila por intento de B1, con la variante y el código del cupón que llevó el mensaje. Inmutable, con RLS y sin acceso de la API.
- `record_payment_failure_first_touch_v1` (nueva, solo `service_role`): el bridge la llama en B1, con el cerco de la lease, antes del envío. Idempotente por intento: el mismo valor da `already_recorded`, otro valor es 55000 `payment_failure_first_touch_record_conflict`.
- `get_payment_failure_followup_context_v1` (nueva, solo `service_role`): con la lease del seguimiento, `followup_kind` (`second_touch` = B2, `last_touch` = Z), `first_touch_variant` y `payment_failure_count`. **La regla:** Z si el registro del intento de B1 que Chatwoot aceptó tiene cupón; si no, o si no hay registro, B2.
- `mark_portable_payment_failure_request_started`: acepta también un `no_reply_review` de una secuencia del pago rechazado. Autoriza igual, contra el scope del caso con `PURCHASE_CANCELED`: el seguimiento cuenta en sus topes.
- `plan_payment_failure_recovery`: un rechazo nuevo durante la secuencia se suma al caso (como antes) y ahora corre también el `expected_case_version` del seguimiento que espera, **sin cambiar su `due_at`**. Antes la reevaluación lo pausaba con `authoritative_state_changed` y pausaba el caso.

## El envío (bridge)

- **Configuración.** `PAYMENT_FAILURE_FOLLOWUP_ENABLED` (apagado por defecto) con `WABA_PAYMENT_FAILURE_TEMPLATE_SECOND_TOUCH` (B2) y `WABA_PAYMENT_FAILURE_TEMPLATE_LAST_TOUCH` (Z), distintas. El idioma es el de las otras plantillas (`WABA_TEMPLATE_LANGUAGE`) y la categoría, MARKETING. Prendido exige `PAYMENT_FAILURE_VARIANTS_ENABLED` y `RECOVERY_COUPON_WORKER_ENABLED`; si falta algo, el bridge no arranca. Las plantillas son del runtime, no del manifiesto, como las de B1 y las del carrito.
- **Composición** (`payment_failure_followups.py`): lee el contexto de la base y elige la plantilla; el resto es la Z del carrito (`compose_cart_last_touch`). El cupón 2 que no está `valid` se espera 8 minutos, hasta dos veces y dentro de los 30 minutos de su hora; uno muerto (`create_failed`, `expired`, `redeemed`) o que no llegó a tiempo cierra sin mensaje (`payment_failure_followup_coupon_not_ready_in_time`). El botón es nuestro link más `&offDiscount=<código>`, y `vence` es el vencimiento real del cupón 2, en la zona del país del teléfono.
- **El arranque** es el del pago rechazado (`mark_portable_payment_failure_request_started`): el flujo del paso sale de su `step_key` (`followup_steps.FOLLOWING_STEP_TRIGGERS`).
- **El registro de B1.** Con las variantes prendidas, B1 registra su variante y su cupón justo antes del envío, con o sin este flag. Si el registro falla (por ejemplo, una base sin la migración), B1 sale igual y queda `payment_failure_first_touch_record_failed` en el log: lo peor que pasa es que el seguimiento salga como B2, que lleva su propio cupón 2. Si B1 cae a la plantilla de hoy, no se registra nada.

## Qué lo corta

Lo mismo que a todo seguimiento: que la persona conteste después de B1 (`prospect_replied`), la compra de cualquier oferta del producto (`20261019000300`), la baja (incluido «No más mensajes»), la derivación o la toma de una persona del equipo, y la pérdida del consentimiento.

## Qué no cambia

B1 (su elección, su cupón, su link y su fallback), el carrito, la ficha, Johanna, la audiencia y la compra. Con el flag apagado, el paso `payment_failure_followup` cierra sin mensaje (`approved_template_direct_unsupported_action`), como todo paso siguiente sin dueño. Con la política de un toque publicada hoy, nada de esto se ejecuta.

## Cómo se prende

1. Aplicar la migración `20261019000700`.
2. Aprobar en Meta las dos plantillas y verificar que estén en el catálogo del inbox.
3. Publicar la política de dos toques y apuntar a ella el scope del pago rechazado (`PAYMENT_FAILURE_FOLLOWUP_POLICY_*`), con B1 ya prendido.
4. `PAYMENT_FAILURE_FOLLOWUP_ENABLED=true` con las dos plantillas, y redeploy.

Si la política de dos toques se publica sin el flag, los seguimientos que inserte el motor cierran sin mensaje: el orden de los pasos 3 y 4 no manda mensajes de más.
