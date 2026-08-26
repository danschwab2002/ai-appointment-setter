# ADR-0016 — Autorización dinámica de destinatarios Johanna

- **Estado:** Aceptada; implementada y verificada localmente, pendiente de publicación, migración y despliegue
- **Fecha:** 2026-08-26
- **Supersede parcialmente:** el fence por `ALLOWED_WHATSAPP_JID` de las rutas productivas Johanna
- **Relacionado:** ADR-0015

## Contexto

El rollout inicial utilizó un único `ALLOWED_WHATSAPP_JID` para impedir contactos accidentales. Esa restricción ya no corresponde al MVP activo: el inbound oficial debe atender a cada remitente real del account/inbox autorizado y los triggers Hotmart deben contactar al teléfono inequívocamente correlacionado y autorizado por estado durable.

Eliminar toda validación de destino sería inseguro. La autorización debe cambiar de una configuración global a una autoridad específica por conversación o command.

## Decisión

`ALLOWED_WHATSAPP_JID` deja de ser requisito y autoridad en las rutas productivas dedicadas de Johanna:

- inbound scoped deriva `expected_jid` del remitente canónico de cada conversación del account `1`, inbox `9`;
- carrito automático deriva `target_phone` dentro de `begin_johanna_abandonment_hotmart_auto_v2`, a partir de la intención durable correlacionada;
- pago fallido conserva su `target_phone` derivado del caso y la intención durables.

Antes de cada efecto, el bridge vuelve a crear un fence local exacto para ese destinatario:

- inbound revalida `expected_jid` contra la conversación;
- carrito y pago fallido construyen un sender efímero cuyo único JID permitido es el teléfono que devolvió el RPC autorizado.

El caller del RPC V2 de carrito no puede aportar ni sustituir el teléfono. El wrapper relee `purchase_intents.normalized_phone`, exige forma canónica y delega al RPC durable existente, que conserva correlación, consentimiento, opt-out, compra, ambigüedad, idempotencia y presupuesto por persona.

`ALLOWED_WHATSAPP_JID` permanece opcional sólo para endpoints manuales/test y motores legacy default-off. Activar una de esas rutas sin el valor canónico continúa fallando cerrado.

## Consecuencias

- Quitar la variable del deployment productivo no bloquea inbound scoped, carrito automático ni pago fallido.
- Dos leads elegibles con teléfonos distintos pueden producir commands independientes.
- Un mismo teléfono conserva un único presupuesto físico compartido entre carrito y pago fallido.
- Un número arbitrario no puede ser elegido por payload HTTP, configuración del modelo ni caller del RPC V2.
- Account/inbox, scope Hotmart, producto/oferta, identidad, consentimiento, opt-out, compra, idempotencia y reconciliación permanecen obligatorios.
- El cambio requiere aplicar la migración antes o junto con la nueva imagen. Durante una actualización descoordinada, la ruta automática falla cerrada; no existe fallback al RPC anterior con teléfono suministrado por la app.

## Alternativas descartadas

### Permitir cualquier teléfono directamente en `ChatwootMessageSender`

Descartada porque convertiría un adapter general en una superficie de envío arbitrario.

### Conservar un JID global y actualizarlo por lead

Descartada porque introduce estado mutable global, carreras y sólo permite un lead a la vez.

### Confiar en el teléfono del webhook Hotmart

Descartada porque una observación del proveedor no equivale a identidad correlacionada ni autorización de efecto.
