# Correlación Hotmart ↔ intención pre-checkout — V1

- **Estado:** Contrato implementado localmente; migración Cloud y E2E oficial pendientes
- **Versión:** `1.0.0`
- **Ámbito:** `lead.precheckout` observado ↔ `PURCHASE_APPROVED` / `PURCHASE_OUT_OF_SHOPPING_CART`
- **Efectos externos:** ninguno

## 1. Fuentes y frontera

La intención nace en `purchase_intents` mediante el adapter observado
`lead.precheckout`. Hotmart conserva autoridad exclusiva sobre dos hechos:

- `PURCHASE_APPROVED`: compra aprobada;
- `PURCHASE_OUT_OF_SHOPPING_CART`: salida oficial del checkout.

No se infiere abandono por silencio ni por tiempo transcurrido. El nombre del comprador
nunca participa en identidad.

Los wrappers `admit_and_correlate_hotmart_purchase_approved` y
`admit_and_correlate_hotmart_cart_abandonment` admiten evento, identidad canónica y
correlación en una sola transacción. El RPC exact-ID
`correlate_hotmart_purchase_intent(uuid)` permite replay controlado y devuelve el ledger
existente sin reescribirlo.

La migración es una fase **expand** compatible con rolling deploy. Las firmas históricas
`admit_hotmart_purchase_approved(text,jsonb)` y
`admit_hotmart_cart_abandonment(text,jsonb)` se conservan temporalmente como shims
seguros: derivan la identidad del payload y delegan en los wrappers correlacionados.
Por eso el orden permitido es migración primero y bridge después; réplicas viejas y
nuevas producen la misma correlación atómica. Las implementaciones base renombradas y
los helpers no son ejecutables por `service_role`. Una futura fase **contract**, sólo
después de comprobar cero réplicas viejas, revocará los dos shims.

## 2. Scope server-side

`hotmart_purchase_intent_scopes` traduce los identificadores que no son equivalentes:

```text
Hotmart product.id              8104005
purchase_intents.product_ref    F106691755G
Hotmart / intent offer_ref      bxjge6zq
tenant_ref                      lancemos
funnel_ref                      psicologajohanna
max_lookback                    24 hours
```

Sólo un scope activo puede poseer una pareja `hotmart_product_id + offer_ref`. La
comparación de identificadores se hace exacta después de `trim + lower`; no existe fuzzy
matching.

## 3. Candidatos

Una intención candidata debe cumplir simultáneamente:

- scope, producto/hotlink y oferta exactos;
- `provider_observed=true`;
- `provisional=false`;
- `lifecycle_state=waiting_for_purchase`;
- `submitted_at` entre `event_observed_at - max_lookback` y
  `event_observed_at`, inclusive;
- coincidencia exacta por email normalizado o teléfono internacional normalizado.

El email se normaliza con `trim + lower`. El adapter valida el teléfono E.164 y la
frontera SQL conserva su representación canónica ya usada por `purchase_intents`:
8–15 dígitos internacionales, sin `+`. Esa identidad se persiste append-only en
`hotmart_purchase_intent_event_identities`; el payload Hotmart crudo no se reescribe.
No se intenta reparar un teléfono durante correlación.

## 4. Outcomes durables

Cada evento produce como máximo una fila append-only en
`hotmart_purchase_intent_correlations` y cero o más candidatos append-only en
`hotmart_purchase_intent_correlation_candidates`.

| Outcome | Condición | Intención resuelta | Handoff manual |
|---|---|---:|---:|
| `resolved` | una única señal disponible identifica un candidato, o email y teléfono identifican el mismo candidato único | sí | no |
| `unmatched` | no existe scope o ningún identificador encuentra candidato | no | sí |
| `ambiguous` | una señal o la intersección deja múltiples candidatos | no | sí |
| `conflict` | email y teléfono no convergen de forma única, incluso si sólo uno encuentra candidato | no | sí |

`matched_by` sólo puede ser `email`, `phone` o `email_and_phone` cuando el outcome es
`resolved`. `unmatched`, `ambiguous` y `conflict` mantienen
`purchase_intent_id=null` y `manual_handoff_required=true`.

## 5. Transiciones

### `PURCHASE_OUT_OF_SHOPPING_CART` resuelto

```text
lifecycle_state            waiting_for_purchase
current_classification     abandonment_candidate
activation_authorized      false
```

Confirma abandono oficial, pero no concede consentimiento ni autoriza contacto.

### `PURCHASE_APPROVED` resuelto

```text
lifecycle_state            purchased
current_classification     null
activation_authorized      false
```

La compra supersede monotónicamente un abandono confirmado previo y bloquea recuperación.

### `ambiguous` o `conflict`

Todos los candidatos quedan con `activation_authorized=false`:

- `ambiguous` → `tracking_incomplete`;
- `conflict` → `identity_conflict`.

`unmatched` no modifica intenciones.

## 6. Idempotencia e inmutabilidad

- `webhook_event_id` es la clave primaria del ledger de correlación;
- un replay exact-ID devuelve el resultado ya persistido;
- correlaciones y candidatos rechazan `UPDATE` y `DELETE`;
- `service_role` puede ejecutar los dos wrappers, el RPC exact-ID y, durante la fase
  expand, los dos shims históricos seguros;
- `anon` y `authenticated` no tienen acceso a tablas ni RPC;
- los helpers internos no son ejecutables por roles API ni por `service_role`;
- un replay con el mismo payload y otra identidad canónica falla y conserva el ledger.

## 7. Exclusiones explícitas

Este contrato no:

- crea `recovery_cases`;
- crea secuencias ni acciones;
- ejecuta dispatcher, AgentBot, WhatsApp o email;
- activa workers generales;
- interpreta pago rechazado o estado incierto;
- prueba todavía un evento Hotmart fresco en Supabase Cloud.
