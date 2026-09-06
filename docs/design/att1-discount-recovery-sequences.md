# Secuencias de recuperación con descuento para ATT1

- **Estado:** Política y planificación post-inbound implementadas default-off; template y activación pendientes
- **Fecha:** 2026-09-01
- **Fuente:** `Documentación de Procesos Carritos Abandonados, Pagos Declinados y Pagos Offline.pdf`, entregada por el usuario
- **Alcance:** diseñar únicamente la política de descuento; no modificar cantidad de mensajes, triggers, delays, cadencia ni condiciones de los flujos ya aprobados
- **Gate comercial consolidado:** [aprobación de información comercial V1](att1-commercial-information-approval-v1.md), decisión `att1-commercial-006-discount`

## Lectura aplicable de la fuente

La fuente describe tres situaciones:

1. **Formulario precheckout sin compra observada.** Después del formulario post-VSL espera 30 minutos; si no existe señal de comprador inicia recuperación por WhatsApp. La primera comunicación ofrece 10% de descuento con validez declarada de 6 horas. Si la persona responde, entrega código, guía visual y enlace directo al carrito (pp. 1–2).
2. **Pago declinado o compra fallida.** Ofrece asistencia inmediata, un medio de pago alternativo o handoff humano. Si durante 48 horas no hay respuesta ni compra, propone un último estímulo con 10% de descuento (pp. 4–5).
3. **Pago offline.** Declara que no existe señal Hotmart confiable y que el proceso no está estructurado (p. 5).

Para el primer caso también propone pausar después de dos días sin respuesta y recordar el descuento cuando la persona vuelva a escribir (p. 3).

## Decisión confirmada

- La cantidad de mensajes, triggers, delays, cadencia, prioridades y condiciones de los flujos permanecen exactamente como están diseñados actualmente.
- El PDF no modifica el grace period vigente, los budgets, los stops ni la mecánica durable existente.
- El único elemento nuevo es una política de descuento durable y versionada.
- Compra, opt-out, takeover, restricción, conflicto de identidad y presupuesto consumido continúan prevaleciendo.

No se adopta automáticamente:

- GHL, sus etiquetas ni su temporizador como fuente de verdad;
- ausencia de etiqueta `Comprador` como prueba de no compra;
- los 30 minutos, 48 horas o dos días del protocolo como cambios a los tiempos vigentes;
- Stripe sin proveedor, enlace y autorización confirmados;
- recuperación pasiva ilimitada;
- pagos offline sin señal autoritativa;
- ningún texto como template Meta aprobado: el PDF describe contenido, pero **no contiene bodies literales de plantillas WhatsApp**.

## Integración con el modelo existente

```text
flujo durable existente sin cambios
→ al construir la acción ya autorizada, resolver política de descuento publicada
→ seleccionar template/copy version aprobados para esa posición existente
→ mantener la misma reserva, presupuesto, request_started y finalización
```

La política puede asociarse a `payment_failure`, `confirmed_cart_abandonment` y `precheckout_without_purchase_signal`. Para `payment_failure`, una respuesta inbound canónica posterior al primer contacto abre una secuencia nueva de un solo mensaje y una acción `inbound_reply_offer`; no existe timer ni acción mientras hay silencio. Los otros dos triggers conservan sólo la representación de política y no fueron conectados por este corte. El caso compartido y la unicidad durable impiden duplicar el contacto adicional.

## Política durable de incentivo

El descuento no vive en prompts ni constantes Python. La frontera mínima implementada fija:

- clave y versión de política;
- tenant, funnel y binding version; producto y oferta se derivan del binding exacto;
- porcentaje o importe;
- referencia del cupón y fuente autorizada del código;
- vigencia y expiración;
- triggers habilitados;
- posición ya existente (`first_touch | later_step`) y template/copy version;
- estado `draft | approved | published | retired`;
- aprobador y timestamps.

Checkout/assets autorizados, budgets existentes y Conversation Release siguen
siendo autoridades separadas. No se duplican dentro de esta tabla mínima ni se
modifican por publicar una política.

Publicar la política no activa envíos. También se requieren binding activo, Conversation Release aprobada, template WABA sincronizado, scope/cohorte, budgets y kill switch.

## Decisión comercial confirmada por el operador

- descuento porcentual de **10 %**;
- triggers: `payment_failure`, `confirmed_cart_abandonment` y
  `precheckout_without_purchase_signal`;
- elegibilidad adicional: al menos una respuesta inbound posterior a la plantilla inicial de inicio de conversación de Meta;
- posición existente: `later_step`;
- entrega: código variable de la plantilla de Meta, sin valor hardcodeado;
- duración: el cupón no vence;
- alcance económico: sin restricciones propias por país o moneda;
- copy: no se permite urgencia, escasez ni vencimiento;
- aprobadora: Marcela, según confirmación reportada por el operador.

El alcance general del cupón no amplía el piloto, que permanece candidato sólo
para México. Consentimiento, opt-out, stops, condiciones y cadencia existentes
siguen siendo determinísticos y no cambian por esta decisión.

La posición no introduce cadencia por tiempo. En `payment_failure`, el evento
inbound crea una acción diferida única; ninguna ausencia de respuesta puede
crearla ni volverla reclamable. Bodies, placeholders, categoría, botones y
assets pertenecen a la Conversation Release y deben coincidir exactamente con
templates `APPROVED` de Meta/Chatwoot.

## Pendientes de implementación

La decisión elimina la contradicción de la fuente: se adopta vigencia abierta y
se descarta la urgencia de seis horas. El texto final de la plantilla, su clave y
el mapeo exacto de la variable del cupón todavía deben aprobarse. La aprobación
de Marcela fue reportada por el operador; su confirmación directa como autoridad
comercial general permanece en el gate consolidado.

La migración `20260903000200_commercial_ally_indefinite_discount.sql` eliminó esa
incompatibilidad: la política puede declarar `offer_expiration_mode = indefinite`,
`offer_valid_for = null`, `requires_inbound_reply_after_initial_template = true`,
`coupon_delivery_mode = meta_template_variable` y `urgency_copy_allowed = false`.
La política seguirá versionada: puede retirarse mediante una transición explícita,
pero no expira automáticamente ni habilita texto de urgencia.

El runtime ya puede crear el `later_step` diferido a partir de una respuesta
inbound canónica para `payment_failure`, bajo un gate default-off. Todavía no
existe template WABA aprobado con su variable ni una transición de activación
que vuelva reclamable la acción. Esos límites bloquean el envío, no la
planificación durable.

## Estado de implementación

Las migraciones `20260901000400_commercial_ally_discount_policies.sql`,
`20260903000200_commercial_ally_indefinite_discount.sql` y
`20260905000100_commercial_ally_post_inbound_discount.sql` implementan la frontera mínima:

- políticas por binding, trigger, clave y versión;
- `draft | approved | published | retired`;
- porcentaje o importe fijo, referencia de cupón, vigencia finita o indefinida y etapa de presentación;
- respuesta inbound requerida, variable de template y prohibición de urgencia;
- template/copy version exactos;
- una sola versión `published` por binding y trigger;
- cero semillas y resolución vacía por defecto;
- runtime sin lectura directa ni DML de tabla; sólo puede ejecutar el resolver de una política publicada, vigente y ligada a un binding activo.
- evidencia inbound sanitizada, binding append-only y una única acción diferida por caso de pago fallido;
- replay del mismo inbound o inbounds posteriores devuelven la misma acción;
- `next_attempt_at = infinity` y `effect_authorized = false` hasta otro release explícito.

La estructura no activa efectos, timers, deploy ni salida a Meta. La política y el template productivos siguen sin publicarse; descuentos/outbound permanecen apagados.

## Evidencia histórica ampliada

En 150 conversaciones de recupero se observaron 123 primeros contactos con 10 %
y vencimiento de seis horas. En 132 de las 150 apareció al menos un conflicto con
las reglas vigentes; siete contenían más de un contacto inicial y 18 agregaban
otro outbound antes de una respuesta. Ninguno de los 32 descuentos detectados
después de inbound estaba libre de urgencia. Estos conteos justifican no reutilizar
las secuencias históricas; no cambian la política aprobada.
