# Los formularios de GHL

**Cómo llega.** Cuando alguien envía un formulario de la landing, un workflow de GHL con el disparador *Form submitted* y la acción *Webhook* le manda un POST al bridge, a `POST /webhooks/adapters/ghl/lead-precheckout` (`src/bridge/app.py`, `GHL_PRECHECKOUT_ADAPTER_PATH`). El adaptador lo traduce al evento canónico `lead.precheckout` (la intención) y lo admite por el mismo camino que un formulario de landing. Contrato: `docs/contracts/ghl-precheckout-adapter-v1.md`.

**El permiso de WhatsApp.** Solo entran los formularios listados en la configuración del adaptador, y cada envío sale con permiso de WhatsApp: listar un formulario afirma que muestra la aclaración del consentimiento. Es el permiso `formulario` de los flujos: sin un formulario con permiso de esa persona, un flujo con `permiso: formulario` no le escribe.

**El mail.** GHL manda el mail del contacto de GHL (`contact.email`), no lo que la persona tipeó en ese envío. Si el teléfono y el mail quedaron en dos contactos distintos de GHL, la intención puede no atarse a la compra.

**Qué dispara.** La intención es el disparador `intencion` del primer contacto. El carrito y el pago rechazado la usan como permiso: buscan el formulario de esa persona antes de escribirle.

**El token.** GHL no firma el pedido: lo autentica un token propio del adaptador. Un pedido sin el token se rechaza sin decir por qué.
