# Contrato `lead.precheckout` V1 — Lancemos → bridge

- **Estado:** V1.0.0 desplegado; V1.1.0 implementado localmente y pendiente de despliegue
- **Versiones externas:** `1.0.0`, `1.1.0`
- **Endpoint:** `POST /webhooks/lead`
- **Emisor previsto:** `/api/lead` server-side de la landing

## Propósito y límite

El evento prueba que una persona presionó **Continuar al pago**. Crea o enlaza una
`purchase_intent`; no prueba que abrió Hotmart, abandonó ni falló un pago. V1.0.0
no concede autorización. V1.1.0 puede aportar evidencia versionada de una
autorización explícita mostrada por la landing, pero no autoriza un request outbound.

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

- ULID y versión exactos; `version` no se recorta ni normaliza y cualquier variante
  devuelve HTTP 400 antes de invocar la RPC;
- relación conocida `landing_id → offer.code`;
- host/path HTTPS de landing;
- email normalizado `lower + trim`;
- composición teléfono = prefijo + número nacional;
- teléfono real por país con `phonenumbers`;
- producto, hotlink, moneda y checkout oficial;
- `dedupe_key = site:offer:email_normalizado`;
- consentimiento exacto según la versión externa.

Los demás textos usan una única regla de borde en parser y RPC: sólo se recorta
whitespace ASCII (`space`, tab, CR/LF, form feed y vertical tab). Whitespace Unicode
no se normaliza implícitamente.

Consentimiento V1.0.0:

```text
marketing_optin=false
notice=<texto no vacío>
```

Consentimiento V1.1.0:

```text
marketing_optin=true
whatsapp_contact=true
copy_version=johanna-precheckout-whatsapp-disclosure-v1
```

El relay server-side fija esos valores después de la interacción correspondiente
en la landing; no toma `copy_version` ni la autoridad desde parámetros libres del
navegador. El HMAC cubre el body exacto.

La RPC vuelve a comprobar que el timestamp, scope, nombre, país, producto, oferta,
precio, moneda, checkout y dedupe raw firmados coincidan con su representación
canónica. También exige que `data.buyer.email` normalizado coincida con
`identity.email` y, cuando el teléfono es válido, que
`buyer.phone == '+' + phone_country_code + phone_national` y que esa composición
coincida con `identity.phone`. Una divergencia
produce `observed_precheckout_raw_canonical_mismatch` o
`observed_precheckout_identity_mismatch` y rollback total.

En V1.0.0, un teléfono presente pero inválido no invalida la intención: se
persiste como `normalized_phone=NULL`, `tracking_incomplete` y sin autoridad. En
V1.1.0, teléfono inválido bloquea la admisión completa.

## Representación durable

La RPC `admit_observed_lead_precheckout` escribe atómicamente:

```text
precheckout_submissions
purchase_intents
purchase_intent_submissions
```

Invariantes V1.0.0:

```text
contract_version=1.0.0
provisional=false
provider_observed=true
activation_authorized=false
whatsapp_contact_authorized=false
```

Invariantes V1.1.0:

```text
contract_version=1.1.0
provisional=false
provider_observed=true
activation_authorized=true
whatsapp_contact_authorized=true
consent.copy_version=johanna-precheckout-whatsapp-disclosure-v1
```

`id` deduplica retries exactos. Un body distinto bajo el mismo ID registra
`semantic_conflict`. Para el alcance inicial, submissions distintas del mismo
email/oferta reutilizan una intención viva. Una V1.1.0 válida puede promover la
misma intención consistente de `false|false` a `true|true`; una V1.0.0 posterior
no revoca esa evidencia porque expresa ausencia de opt-in, no opt-out. Un teléfono
contradictorio marca `identity_conflict`, revoca ambas marcas locales y no concede
contacto.

Una correlación `resolved` de `PURCHASE_OUT_OF_SHOPPING_CART` conserva las marcas
V1.1.0 para que el timer pueda reevaluarlas. Compra, `conflict`, `ambiguous`,
opt-out, takeover y cualquier restricción autoritativa siguen prevaleciendo.

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

El emisor puede reintentar el mismo `id`; los retries son idempotentes. En este
corte, la respuesta conserva `activation_authorized=false` y
`contact_authorized=false`: no pretende proyectar el estado durable final porque
una colisión de identidad puede negar la autoridad dentro de la transacción. La
fuente autoritativa es `purchase_intents`, no la respuesta de transporte.

## Diferencias deliberadas respecto del documento fuente

1. Hotmart mantiene `POST /webhooks/hotmart` y Hottok. Los eventos se normalizan
   internamente, pero no comparten la puerta ni el secreto de la landing.
2. Silencio después del pre-checkout no autoriza inferir abandono ni enviar.
3. V1.0.0 bloquea todo contacto proactivo; V1.1.0 sólo supera el gate local de
   autorización y continúa bloqueado por las fronteras comerciales posteriores.
4. La respuesta llega después de persistir durably; no se usa una cola en memoria.
5. No existe fallback a email en este corte: teléfono inválido queda para revisión.
6. El wording concreto se administra en la landing. El relay sólo puede emitir
   V1.1.0 mientras ese wording corresponda a la `copy_version` contractual.
