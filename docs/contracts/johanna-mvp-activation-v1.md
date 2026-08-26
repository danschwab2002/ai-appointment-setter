# Contrato de activación del MVP de Johanna V1

- **Estado:** Implementado y verificado localmente; publicación, migración, despliegue y activación productiva pendientes
- **Versión:** 1
- **Scope inbound público:** Chatwoot account `1`, inbox `9`, `libre-de-ansiedad-inbound` versión `2`
- **Scope Hotmart:** producto `8104005`, oferta `bxjge6zq`, payload `2.0.0`

## Propósito

Este contrato reúne las tres situaciones ejecutables del MVP de Johanna sin convertirlas en un motor general de follow-ups:

1. carrito abandonado con first-touch WABA ya existente;
2. pago rechazado soportado con first-touch WABA durable y acotado;
3. atención comercial inbound con respuesta automatizada o handoff durable.

Que una situación esté admitida no implica que tenga un efecto saliente. Cada ruta declara su propio efecto.

## Gates de activación

Los flags nuevos son default-off:

```text
CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED=false
JOHANNA_PAYMENT_FAILURE_HOTMART_ENABLED=false
JOHANNA_PAYMENT_FAILURE_OUTBOUND_ENABLED=false
```

El modo inbound scoped sólo puede arrancar cuando coinciden todos estos valores:

- account `1`;
- inbox `9`;
- scope key `libre-de-ansiedad-inbound`;
- scope version `2`;
- admisión Cut B activa;
- agente Cut B y replies automáticos activos;
- opt-out durable activo;
- pausa humana activa;
- admisión y proyección de handoff activas.

Una combinación incompleta o un scope distinto detiene el arranque. Dispatcher, outbound durable, timers y follow-ups generales conservan gates independientes.

El outbound de pago fallido sólo puede arrancar cuando admisión y outbound están
activos simultáneamente y el runtime coincide con account `1`, inbox `9`, provider
`waba` y channel account `chatwoot-inbox:9`. Para activar en EasyPanel, primero se
aplica/verifica la migración y después se fijan explícitamente:

```text
JOHANNA_PAYMENT_FAILURE_HOTMART_ENABLED=true
JOHANNA_PAYMENT_FAILURE_OUTBOUND_ENABLED=true
CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED=true
```

## Inbound comercial

Después de validar firma, freshness, account e inbox, el bridge acepta un remitente sólo si puede canonicalizarlo estrictamente como:

```text
[1-9][0-9]{6,14}
```

Se aceptan únicamente la forma decimal o la misma identidad con sufijo `@s.whatsapp.net`. La identidad canónica observada queda ligada al work item y se vuelve a validar contra la conversación exacta antes de:

- leer historia canónica;
- enviar un reply AgentBot;
- validar autoridad para un handoff;
- asignar la conversación;
- crear la nota privada de handoff.

Una identidad faltante, malformada o divergente corta el flujo. `evidence_conflict` tampoco invoca Hermes ni produce reply.

El handoff primero persiste el stop durable. Si se decide handoff, no se envía además un reply automático. Assignment y nota privada son proyecciones reconciliables; su fallo no reanuda automatización.

## Pago fallido soportado

El receiver sólo procesa esta conjunción exacta:

```text
event = PURCHASE_CANCELED
version = 2.0.0
data.purchase.status = CANCELLED
data.purchase.payment.refusal_reason = NO_FUNDS
```

Además exige:

- producto numérico `8104005`;
- oferta `bxjge6zq`;
- transaction alfanumérica válida;
- timestamp positivo;
- email o teléfono canónico derivado del payload;
- igualdad exacta entre identidades derivadas en Python y las recibidas por la RPC.

`PURCHASE_CANCELED` sin la razón soportada se ignora; no se interpreta como fallo de pago.

### Admisión durable

`admit_johanna_payment_failure`:

- deriva nuevamente todas las dimensiones desde el payload validado;
- correlaciona sólo con intents activos del namespace interno
  `lancemos / psicologajohanna / ads-a / f106691755g / bxjge6zq` y con el producto
  Hotmart externo `8104005`;
- devuelve `resolved`, `unmatched`, `ambiguous` o `conflict`;
- marca `payment_failure_supported` únicamente al resolver una identidad consistente;
- crea un caso `pending_human_review`;
- devuelve la misma fila en replay exacto;
- devuelve `semantic_conflict` sin crear una segunda fila si el mismo event ID cambia semánticamente.

`PURCHASE_APPROVED` conserva precedencia: intents ya comprados no son candidatos para recuperación.

### Efecto

Si la correlación es `resolved` y el flag outbound está activo,
`begin_johanna_payment_failure_hotmart_auto` vuelve a leer y bloquear el caso, el
intent, el scope y el destinatario antes de crear `request_started`. Exige:

- intent todavía `waiting_for_purchase` y clasificado `payment_failure_supported`;
- `provider_observed=true`, `provisional=false` y activación durable explícita;
- consentimiento comercial y WhatsApp bajo
  `johanna-precheckout-whatsapp-disclosure-v1`;
- teléfono del intent igual al caso admitido;
- ausencia de opt-out durable o contacto bloqueado/restringido;
- un único propietario canónico del teléfono;
- ventana máxima de 24 horas entre intent y rechazo.

El command fija exactamente:

```text
template_name = johanna_compra_fallida_01
template_language = es_EC
template_category = MARKETING
copy_version = johanna-payment-failure-one-shot-v1
max_messages = 1
followups_allowed = 0
```

Carrito y pago fallido escriben en el mismo ledger físico y comparten el unique
budget por teléfono. Si cualquiera ya reservó contacto, el otro devuelve
`budget_consumed` y no crea un segundo command. `PURCHASE_APPROVED` que llega antes
de `request_started` cambia el intent a comprado y elimina autoridad para recuperar
el pago. Después de `request_started`, una respuesta ambigua nunca se reenvía a
ciegas: queda `delivery_unknown` hasta reconciliación.

Chatwoot acepta la plantilla mediante un sender construido para el teléfono que
la RPC acaba de autorizar. El bridge valida nombre, idioma, categoría y versión de
copy antes del POST. La finalización común persiste `outbound_accepted` o
`delivery_unknown` también en el caso de pago fallido.

## Carrito abandonado

La ruta `PURCHASE_OUT_OF_SHOPPING_CART → begin_johanna_abandonment_hotmart_auto_v2`
no recibe un teléfono del caller. El RPC deriva el destinatario desde la intención
correlacionada, valida forma canónica y delega en la reserva durable existente. El
bridge construye después un sender cuyo único JID permitido corresponde exactamente
a ese `target_phone`.

`ALLOWED_WHATSAPP_JID` no limita inbound scoped, carrito automático ni pago fallido y
puede estar ausente en esa configuración productiva. Sigue siendo obligatorio sólo
para endpoints manuales/test y motores legacy que permanezcan habilitados. El ledger
de carrito continúa siendo la frontera de presupuesto compartida con pago fallido:
un mensaje máximo por teléfono entre ambos triggers. Ver [contrato one-shot de
abandono V1](johanna-abandonment-one-shot-v1.md) y [ADR-0016](../decisions/0016-durable-dynamic-recipient-authorization.md).

## HTTP

Para pago fallido habilitado:

- `202`: caso nuevo admitido y plantilla aceptada por Chatwoot cuando es elegible;
- `200`: replay terminal o presupuesto ya consumido;
- `409`: conflicto semántico;
- `422`: payload autenticado pero no soportado;
- `502`: Chatwoot no confirmó la aceptación del efecto;
- `503`: Supabase/RPC no disponible.

El response sólo expone estados estables e IDs internos. No incluye payload, email, teléfono, Hottok ni firmas.

## ACL

`johanna_payment_failure_cases` no concede acceso directo a roles API ni a
`service_role`. Tanto admisión como begin son `SECURITY DEFINER`, fijan
`search_path=pg_catalog, public, pg_temp`, revocan `PUBLIC`, `anon` y
`authenticated`, y conceden sólo `EXECUTE` a `service_role`.

El claim de proyección de handoff devuelve `external_user_id`; el worker lo convierte al JID esperado y Chatwoot revalida esa identidad antes de cada mutación.

## Evidencia requerida para cambiar de estado

- **Implementado:** pruebas focales y suite completa del repo.
- **Publicado:** commit revisado, PR y CI verdes.
- **Migrado:** migración `20260825000500` aplicada y verificada independientemente.
- **Desplegado:** runtime en el commit mergeado, health/readiness `200`.
- **Activado:** flags exactos activos y startup válido.
- **Observado:** tráfico real elegible registrado duramente.
- **E2E:** efecto físico confirmado con un contacto elegible nuevo; el contacto de
  la evidencia anterior no se reutiliza.
