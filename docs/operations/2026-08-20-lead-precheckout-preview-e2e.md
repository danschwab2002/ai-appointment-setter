# Evidencia E2E del relay preview `lead.precheckout` — 2026-08-20

- **Estado:** Evidencia operativa
- **Alcance:** preview Vercel de `ads-a` → Appointment Bridge → Supabase Cloud
- **Outbound:** deshabilitado

## Corte verificado

```text
preview autenticado de Vercel
→ POST same-origin /api/lead
→ relay server-side
→ POST /webhooks/lead
→ validación HMAC contractual
→ admisión durable en Supabase Cloud
```

La prueba evitó el submit normal del formulario, Zapier, píxeles y redirección a
Hotmart. Se usaron datos sintéticos y no se registran en este documento.

## Evidencia observada

El relay informó entrega exitosa en el primer intento. Supabase Cloud registró
la submission a `2026-08-20 14:28:11.238403+00` y la vinculó con un
`purchase_intent` durable.

Estado resultante:

```text
provider_observed=true
provisional=false
activation_authorized=false
whatsapp_contact_authorized=false
lifecycle_state=waiting_for_purchase
current_classification=null
```

Comprobaciones posteriores al ingreso:

```text
first_touch_commands=0
outbound_authorizations=0
scheduled_actions=0
delivery_attempts=0
outbound_messages=0
```

## Límites de esta evidencia

Esta prueba confirma recepción, autenticación y persistencia durable sin
efectos salientes. No confirma todavía correlación con un evento Hotmart fresco,
clasificación transaccional ni comportamiento del submit completo del formulario.
