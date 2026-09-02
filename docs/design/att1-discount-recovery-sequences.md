# Secuencias de recuperación con descuento para ATT1

- **Estado:** Frontera mínima de política implementada localmente; contenido y activación pendientes
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

La política puede asociarse a las posiciones ya existentes de `precheckout_without_purchase_signal` y `confirmed_cart_abandonment`, sin crear nuevas acciones, timers o secuencias. El caso y presupuesto compartidos continúan evitando doble contacto.

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

## Decisiones de política pendientes

La política económica permanece completamente sin publicar hasta aprobar en
conjunto:

- porcentaje o importe exacto;
- triggers autorizados;
- posición existente (`first_touch` o `later_step`);
- cupón o fuente canónica del código;
- inicio y duración exacta;
- países y monedas aplicables;
- texto permitido sobre urgencia;
- approver y vigencia de la decisión.

La posición sólo cambia el contenido/template asociado a un paso existente, no
la mecánica ni la cadencia del flujo. Bodies, placeholders, categoría, botones y
assets pertenecen a la Conversation Release y deben coincidir exactamente con
templates `APPROVED` de Meta/Chatwoot.

## Inconsistencia a resolver

La fuente dice que el bono vence **estrictamente a las 6 horas** (p. 2), pero
que después de dos días y en cualquier contacto posterior el bono **sigue
activo** (p. 3). No pueden ser verdad simultáneamente. La duración debe quedar
resuelta aunque el primer copy no la mencione, porque gobierna la elegibilidad
durable del incentivo.

- **Recomendación:** oferta inicial válida 6 horas; cualquier incentivo posterior es una nueva oferta/version autorizada, no una extensión silenciosa.
- Alternativa: vigencia abierta hasta compra/retirada, eliminando la urgencia de 6 horas.

Hasta resolver todos los campos anteriores, ninguna política se publica y
ninguna plantilla puede afirmar vencimiento, permanencia o elegibilidad.

## Estado de implementación

La migración local `20260901000400_commercial_ally_discount_policies.sql` implementa la frontera mínima:

- políticas por binding, trigger, clave y versión;
- `draft | approved | published | retired`;
- porcentaje o importe fijo, referencia de cupón, vigencia y etapa de presentación;
- template/copy version exactos;
- una sola versión `published` por binding y trigger;
- cero semillas y resolución vacía por defecto;
- runtime sin lectura directa ni DML de tabla; sólo puede ejecutar el resolver de una política publicada, vigente y ligada a un binding activo.

La estructura no modifica efectos, mensajes, timers, cadencia, deploy ni activación. No existe aún ninguna política publicada y descuentos/outbound permanecen apagados.
