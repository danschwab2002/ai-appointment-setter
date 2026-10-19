# Contrato: los recordatorios del vencimiento de la ficha de pago (v1)

- **Estado:** implementado en el árbol (rama `feat/claude-att1-ficha-recordatorios-v1`), sin desplegar ni activar. Están la migración `20261019000400`, `src/bridge/payment_slip_reminders.py`, el paso opcional de `compose_payment_slip_first_touch`, el cableado en el dispatcher, el sender y el arranque del bridge, con sus pruebas.
- **Fecha:** 2026-10-19
- **Alcance:** una instancia con la ficha de pago prendida ([portable-payment-slip-first-contact-v1.md](portable-payment-slip-first-contact-v1.md)) y una política de la ficha con más de un paso. Después de C1, la ficha recibe dos recordatorios: C2 el día anterior al vencimiento y C3 el día del vencimiento.
- **Relacionados:** [portable-payment-slip-first-contact-v1.md](portable-payment-slip-first-contact-v1.md) (C1, el que este contrato extiende), [approved-template-direct-dispatch-v1.md](approved-template-direct-dispatch-v1.md), [lancemos-pilot-boundary-runtime-v1.md](lancemos-pilot-boundary-runtime-v1.md). La especificación es la 2b de «los flujos comerciales de ATT1 como tienen que quedar» (2026-10-17), fuera del repo.

## Propósito y límite

Una ficha de OXXO vence a los 3 días de generada. C1 la deja a mano una hora después; si la persona no la paga, la venta se pierde el día del vencimiento. Este contrato suma los dos recordatorios que pide la especificación, con la ficha en el botón:

- **C2**, el paso `payment_slip_due_eve`: el día anterior al vencimiento, desde las 10:00 de la zona del país de la ficha. En ATT1, `att1_ficha_vence_manana_01` (MARKETING, por recategorización de Meta), con nombre, tienda, producto y vencimiento.
- **C3**, el paso `payment_slip_due_day`: el día del vencimiento, desde las 09:00. En ATT1, `att1_ficha_vence_hoy_01` (UTILITY), con nombre, tienda y producto.

No están acá: C4 (la ficha vencida con cupón), Z, C5 (la confirmación del pago) ni que el recordatorio salga el día que la persona eligió.

## Cómo entra cada paso

Los pasos son datos de la política del scope de la ficha, después de `payment_slip_first_contact`:

```json
[
  {"step_key": "payment_slip_first_contact", "mode": "freeform"},
  {"step_key": "payment_slip_due_eve", "mode": "freeform", "delay": "1 minute"},
  {"step_key": "payment_slip_due_day", "mode": "freeform", "delay": "2 minutes"}
]
```

con `max_automatic_messages = 3` y un `expires_after` que llegue al día del vencimiento pasadas las 09:00 (en ATT1, 4 días: el peor caso es una ficha generada de madrugada, cuyo C3 sale el tercer día a las 09:00, unas 81 h después; los 4 días dejan margen para la ventana de envío y los reintentos).

- Al aceptarse un toque, `_finalize_followup_delivery_attempt` (20260813000100) inserta el paso siguiente como `no_reply_review`, anclado al mensaje aceptado (`anchor_type = 'accepted_outbound_message'`), en la conversación del caso, con el mismo `expires_at` y `due_at` = la aceptación de C1 más el `delay`. El delay es corto a propósito: el momento real lo decide el bridge.
- **El flujo de un paso siguiente sale de su `step_key`** (`FOLLOWING_STEP_TRIGGERS`), porque su ancla ya no dice de qué flujo es. El primer toque sigue decidiendo por su ancla. Un `step_key` que nadie declaró conserva el ancla, como antes.
- **El momento.** Al componer, después de leer la ficha y resolver su fila de `[[ficha.medios]]`, el bridge calcula el día del paso (el vencimiento menos 1, o el vencimiento) en la zona de la fila:
  - antes de la hora del paso, el intento se difiere **una vez**, hasta ese momento exacto (`payment_slip_step_not_due`, con `next_attempt_at` en el momento). Cada diferimiento gasta uno de los `max_execution_retries` (3 por defecto), así que quedan dos: uno para mandar y uno de reserva;
  - ese día, desde la hora, sale;
  - un día después, no sale y no se reintenta (`payment_slip_step_late`). Pasa solo si C1 salió con más de un día de atraso: un «vence mañana» otro día sería falso. La secuencia termina ahí.
- La ficha vencida no se manda, como en C1 (`payment_slip_expired`).
- No hay distancia mínima entre dos toques: con OXXO (3 días) C2 sale al menos un día después de C1, salvo que C1 haya salido con atraso. Una fila de `[[ficha.medios]]` con otra vigencia necesita su propia política: con más de 4 días, C2 vence antes de su momento (`retry_beyond_expiration`).
- El horario de la política (en ATT1, de 09:00 a 21:00) sigue rigiendo: fuera de él la reevaluación difiere sin gastar reintentos.

## La migración `20261019000400`

Un «recordatorio de la ficha» es un `no_reply_review` cuya secuencia es de una ficha (`followup_sequences.reason = 'payment_slip'`). Cuatro funciones lo reconocen, cada una redefinida desde su definición viva reemplazando un texto exacto que tiene que aparecer una vez (si no, aborta con `55000`). Firmas y permisos sin cambios:

1. **`get_portable_payment_slip_send_context`** (el bridge lee la ficha para componer) y 2. **`mark_portable_payment_slip_request_started`** (el último cerco antes del envío, que autoriza contra el scope de la ficha con `PURCHASE_BILLET_PRINTED`) exigían `anchor_type = 'payment_slip'`; ahora aceptan también el recordatorio. Sin esto C2 no podía leer la ficha, y por la RPC compartida del carrito se autorizaría con `PURCHASE_OUT_OF_SHOPPING_CART` y el scope de la ficha lo rechazaría.
3. **`reevaluate_followup_action`** no pausa el recordatorio por la ventana de 24 h (`can_reply = false`) ni por una conversación resuelta. Es una plantilla aprobada, que Meta acepta fuera de la ventana, y después de C1 la ventana está cerrada salvo que la persona haya escrito: sin esto, C2 quedaba en `pause` y el caso abierto para siempre. Medido el 2026-10-19 en el inbox 11: de 221 conversaciones sin respuesta a la plantilla después de 24 h, 166 seguían abiertas y 55 las había resuelto una persona del equipo (no hay resolución automática), con una mediana de 16,7 h.
4. **`admit_portable_inbound_commercial_case_v1`** (la adopción de la respuesta a una plantilla) no cuenta el recordatorio como paso pendiente. C2 espera su día cerca de un día y medio, y en ese rato quien contesta C1 no llegaría al agente (el `22000` del 09/10, `20261009000200`). `delivery_unknown` sigue frenando.

## El envío

- La plantilla de cada paso sale de `WhatsAppTemplateConfig.payment_slip_steps`, una por `step_key`, armada al arrancar desde el manifiesto (`[plantillas].ficha_vence_manana` y `ficha_vence_hoy`) y las variables `WABA_PAYMENT_SLIP_DUE_EVE_*` y `WABA_PAYMENT_SLIP_DUE_DAY_*`. Un paso sin su plantilla cierra sin mensaje (`first_touch_template_not_configured`).
- El cuerpo, las variables y el botón se arman igual que C1 (`compose_payment_slip_first_touch` con `step`): el catálogo del inbox se lee en cada envío y tiene que cerrar con la categoría declarada, las variables y el botón de URL con la base de la fila.
- Va a la conversación de C1 por `send_followup`, que ahora acepta los `template_params` armados: sin ellos, el seguimiento es el de siempre. Sale como el AgentBot, como todo seguimiento (no pausa la conversación). El cerco final de Meta lo registra como `followup` con el nombre de la plantilla del paso.
- Cuenta en los topes del scope de la ficha: cada envío es un arranque (`pilot_outbound_request_authorizations`).

## Qué lo corta

- La compra de cualquier oferta del producto (20261019000300), la baja, la derivación a una persona y la pérdida del consentimiento, como en C1.
- **Que la persona conteste** después del toque anterior (`prospect_replied`, la reevaluación compartida). El agente sí toma esa respuesta: el recordatorio pendiente no frena la adopción. ⚠ La especificación pide lo contrario: el recordatorio del vencimiento es utilidad sobre un pago que la persona generó, y tendría que seguir hasta un «no», la baja o la compra. Queda para una versión siguiente: hace falta que la reevaluación distinga estos pasos y que la adopción de la respuesta por el agente no cierre el caso.
- La automatización pausada, la conversación en espera (`snoozed`) o con actividad humana después del toque: se pausa. **La resuelta no lo frena** (una persona del equipo la cerró sin respuesta): es la decisión por defecto de esta versión, para revisar.

## Pruebas

- `tests/test_payment_slip_reminders.py`: el momento de cada paso, el dispatcher con el catálogo capturado (`chatwoot_message_templates_inbox_11_ficha_recordatorios_20261019.json`) y la ficha capturada (espera, envío, tarde, vencida, sin plantilla, otro flujo), la configuración y el sender.
- `tests/test_payment_slip_reminders_wiring.py`: el manifiesto y el arranque.
- `tests/sql/followup_engine/validate_portable_payment_slip_reminders.mjs`: la migración (cambia solo esas cuatro funciones), C1 → C2 → C3 con las RPC reales y la ventana de 24 h cerrada hasta `policy_exhausted` con 3 arranques en el scope de la ficha, C3 con la conversación resuelta (sale) y en espera (se pausa), el predicado contra una secuencia de otro flujo (RPC rechazadas, ventana cerrada pausa, adopción frenada), la adopción de la respuesta con C2 esperando y la cancelación por respuesta.
