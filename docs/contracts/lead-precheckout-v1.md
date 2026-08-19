# Contrato `lead.precheckout` V1 — Lancemos → bridge

- **Estado:** Implementado localmente, default-off; no desplegado ni conectado
- **Versión externa:** `1.0.0`
- **Endpoint:** `POST /webhooks/lead`
- **Emisor previsto:** `/api/lead` server-side de la landing

## Propósito y límite

El evento prueba que una persona presionó **Continuar al pago**. Crea o enlaza una
`purchase_intent`; no prueba que abrió Hotmart, abandonó, falló un pago ni autorizó
mensajes comerciales.

La ausencia de un evento posterior nunca se convierte en abandono. Sólo
`PURCHASE_OUT_OF_SHOPPING_CART` confirma abandono y sólo `PURCHASE_APPROVED`
confirma compra. Un vencimiento sin señal oficial queda desconocido o requiere
revisión humana.

## Transporte autenticado

```text
POST /webhooks/lead
Content-Type: application/json; charset=utf-8
User-Agent: lancemos-lead-relay/1.0
X-Lancemos-Event: lead.precheckout
X-Lancemos-Delivery: <mismo ULID que body.id>
X-Lancemos-Signature: sha256=<HMAC-SHA256 hex del body crudo>
```

El bridge limita el body a 64 KiB, calcula el HMAC sobre los bytes recibidos y
usa comparación constante antes de parsear JSON. Después valida que ambos
headers de identidad coincidan con el body y que `created_at` esté dentro de la
ventana configurada.

El secreto vive sólo en los runtimes server-side. Nunca se expone en JavaScript
del navegador, query strings, logs o Git.

## Alcance implementado

Aunque el parser conoce los seis pares landing-oferta congelados, el endpoint
sólo admite inicialmente:

```text
site       = psicologajohanna
landing_id = ads-a
offer.code = bxjge6zq
hotlink    = F106691755G
```

Las otras cinco variantes fallan con `403 lead_precheckout_outside_scope` hasta
confirmar precio y oferta. Expandir el alcance requiere reemplazar la
configuración, no eliminarla.

## Payload

Se aceptan exactamente las claves declaradas por el contrato externo:

```text
id, event, version, created_at, source, data, dedupe_key
```

El adapter valida recursivamente:

- ULID y versión exactos;
- relación conocida `landing_id → offer.code`;
- host/path HTTPS de landing;
- email normalizado `lower + trim`;
- composición teléfono = prefijo + número nacional;
- teléfono real por país con `phonenumbers`;
- producto, hotlink, moneda y checkout oficial;
- `dedupe_key = site:offer:email_normalizado`;
- `marketing_optin=false` para esta versión.

Un teléfono presente pero inválido no invalida la intención. Se persiste como
`normalized_phone=NULL`, `tracking_incomplete` y sin autoridad de contacto.

## Representación durable

La RPC `admit_observed_lead_precheckout` escribe atómicamente:

```text
precheckout_submissions
purchase_intents
purchase_intent_submissions
```

Invariantes:

```text
contract_version=1.0.0
provisional=false
provider_observed=true
activation_authorized=false
whatsapp_contact_authorized=false
```

`id` deduplica retries exactos. Un body distinto bajo el mismo ID registra
`semantic_conflict`. Para el alcance inicial, submissions distintas del mismo
email/oferta reutilizan una intención viva. Un teléfono contradictorio marca
`identity_conflict` y no concede contacto.

La RPC es `SECURITY DEFINER`, fija `search_path` y sólo `service_role` recibe
`EXECUTE`. La admisión no crea acciones, secuencias, mensajes ni llamadas a
Hermes.

## Respuestas

| HTTP | Resultado |
|---|---|
| `200` | `received`, `duplicate` o `conflict`, después de admisión durable |
| `400` | JSON, forma o headers incoherentes |
| `401` | firma inválida o evento stale/futuro |
| `403` | evento válido fuera del scope activo |
| `413` | body mayor a 64 KiB |
| `503` | receiver apagado/configuración o persistencia no disponible |

El emisor puede reintentar el mismo `id`; los retries son idempotentes.

## Diferencias deliberadas respecto del documento fuente

1. Hotmart mantiene `POST /webhooks/hotmart` y Hottok. Los eventos se normalizan
   internamente, pero no comparten la puerta ni el secreto de la landing.
2. Silencio después del pre-checkout no autoriza inferir abandono ni enviar.
3. `marketing_optin=false` bloquea todo contacto proactivo.
4. La respuesta llega después de persistir durably; no se usa una cola en memoria.
5. No existe fallback a email en este corte: teléfono inválido queda para revisión.
