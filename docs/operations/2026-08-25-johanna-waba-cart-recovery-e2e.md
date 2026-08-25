# E2E Johanna WABA de recuperación de carrito — 2026-08-25

- **Estado:** recepción física confirmada; cierre durable y release en curso
- **Alcance:** un contacto allowlisted, una plantilla, un mensaje, cero follow-ups

## Evidencia observada

El receiver admitió un `lead.precheckout` V1.1.0 con consentimiento WhatsApp válido. El comando one-shot reservó el presupuesto singleton y llegó al sender. La primera ejecución devolvió `502` y se conservó como `delivery_unknown`, sin retry ciego.

La inspección de Chatwoot demostró que el contacto existía pero no se había creado una conversación. El endpoint de creación WABA requería el `source_id` del vínculo contacto–inbox además de `contact_id` e `inbox_id`. Con ese payload se creó una única conversación y se publicó `johanna_carrito_abandonado_01` (`es_EC`, `MARKETING`). Chatwoot devolvió conversación `42` y mensaje `1714`; el receptor confirmó la llegada física.

No se ejecutó un segundo mensaje después de la confirmación física.

## Remediación versionada

La release agrega:

- resolución estricta de `(contact_id, source_id)` para el inbox esperado;
- relectura del vínculo después de crear un contacto;
- `source_id` obligatorio al crear conversación;
- reconciliación service-role-only de `delivery_unknown` a `accepted_by_chatwoot`, sin side effect externo;
- política V2 `johanna-abandonment-single-touch-e2e` con un único paso `approved_template`;
- scope V2 inmutable para `PURCHASE_OUT_OF_SHOPPING_CART`, inbox `9`, producto/oferta exactos,
  cohorte máxima de un contacto y presupuesto total/diario de un request-start;
- runtime promovido V1→V2 por la frontera durable, en `inactive`, generación `1`;
- trigger Hotmart dedicado y default-off que sólo reserva V2 después de una correlación durable
  exacta, sin encender los workers ni el outbound general;
- replay de webhook ligado al mismo command key, con delta cero después de aceptación.
- presupuesto único por teléfono compartido entre V1/V2; la recepción física ya confirmada
  bloquea cualquier segundo command para ese contacto, aunque cambie la intención Hotmart.

La verificación local incluye 1095 tests, el stack completo de 37 migraciones en PostgreSQL 17,
opt-out race bloqueada, reconciliación idempotente, reserva V2/replay con delta cero y ACL cerrada
en 110 funciones públicas / 43 entrypoints service-role.

## Límites de la evidencia

La recepción confirma el canal físico first-touch y la plantilla. No demuestra todavía una entrega originada oficialmente por Hotmart. Esa evidencia requiere un futuro `PURCHASE_OUT_OF_SHOPPING_CART` real; no debe simularse ni provocar otro mensaje al contacto ya alcanzado sólo para cerrar documentación.
