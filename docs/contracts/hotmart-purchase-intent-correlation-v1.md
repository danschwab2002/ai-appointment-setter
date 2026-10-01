# Correlación Hotmart ↔ intención pre-checkout — V1

- **Estado:** Correlación y contract aplicados/verificados en Cloud; delivery originado
  oficialmente por Hotmart pendiente
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

La migración inicial fue una fase **expand** compatible con rolling deploy. Las firmas históricas
`admit_hotmart_purchase_approved(text,jsonb)` y
`admit_hotmart_cart_abandonment(text,jsonb)` se conservaron temporalmente como shims
seguros: derivan la identidad del payload y delegan en los wrappers correlacionados.
Para **expand**, el orden permitido fue migración primero y bridge después; réplicas
viejas y nuevas producían la misma correlación atómica. Las implementaciones base
renombradas y los helpers no son ejecutables por `service_role`.

Para **contract**, el orden es deliberadamente inverso y obligatorio:

1. desplegar el bridge que ya no expone métodos legacy;
2. verificar imagen/task sanos y cero réplicas o servicios legacy activos;
3. aplicar `20260820000400` para revocar ambos shims de `service_role`;
4. comprobar rechazo legacy, wrappers correlacionados operativos y delta comercial cero.

Los cuatro pasos contract se ejecutaron en ese orden. La imagen contract quedó sana con
un único task antes de aplicar `20260820000400`; el postflight confirmó ambos shims en
403, ambos wrappers alcanzando validación, cero filas de probe y delta comercial cero.
Los wrappers correlacionados son las únicas fronteras Hotmart autorizadas.

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
current_classification     confirmed_abandonment
activation_authorized      conserva el valor previo de la intención
```

Confirma abandono oficial, pero no concede consentimiento por sí mismo. Una intención
V1.0.0 permanece en `false`; una intención V1.1.0 ya autorizada conserva `true`. Compra,
`ambiguous`, `conflict`, identidad conflictiva y cualquier bloqueo posterior siguen
revocando o impidiendo activación.

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
- después de aplicar `20260820000400`, `service_role` puede ejecutar los dos wrappers y
  el RPC exact-ID, pero no los dos shims históricos;
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
- acredita procedencia oficial de una entrega emitida por Hotmart.

## 8. Correlador portable (2026-10-01, migración `20261001000100`)

Lo anterior describe el correlador compartido, que usa Johanna y **no cambia**: su coincidencia de teléfono sigue siendo exacta (§3).

Un runtime portable (con manifiesto) correlaciona con una copia derivada:

```text
public._correlate_portable_hotmart_purchase_intent(p_webhook_event_id uuid)
```

- **Qué cambia.** Las dos comparaciones del teléfono pasan a ser en forma canónica: `521` + 10 dígitos equivale a `52` + 10 (México) y `549` + 10 a `54` + 10 (Argentina). El formulario guarda la forma sin el `1` o el `9`; Hotmart, la otra. Con la comparación exacta, un evento que coincidía por email y no por teléfono daba `conflict` y dejaba la intención en `identity_conflict`. Todo lo demás (scope, candidatos, ventana, outcomes, transiciones) es lo de §2 a §5.
- **Cómo se deriva.** La definición vigente del correlador compartido no está en un solo archivo (es la de `20260820000100` más parches posteriores), así que la migración la lee con `pg_get_functiondef` y reemplaza el nombre y las dos comparaciones, exigiendo las ocurrencias exactas. Si la definición compartida no es la esperada, la migración falla con `55000` y no deja nada. Un cambio futuro al correlador compartido no llega solo a la copia: hay que volver a derivarla.
- **El ledger es el mismo.** Escribe en `hotmart_purchase_intent_correlations`, así que un replay por el RPC exact-ID devuelve esa fila. En un match por la otra forma, `matched_by` y `reason_code` dicen lo mismo que en uno exacto (`reason_code` sigue siendo `exact_phone` o `exact_email_and_phone`); no hay un valor nuevo.
- **Quién lo llama.** `admit_portable_hotmart_cart_abandonment` y `admit_portable_hotmart_payment_failure`. No es un entrypoint: ningún rol de la API lo ejecuta, `service_role` incluido. La compra aprobada portable tiene su propia correlación (`admit_portable_hotmart_purchase_approved`, [commercial-ally-runtime-v1.md](commercial-ally-runtime-v1.md)), que compara el teléfono con la misma regla.
- **Nada se reescribe al guardar.** `hotmart_purchase_intent_event_identities` y `purchase_intents` conservan el teléfono como llegó.
- **Brasil** (el noveno dígito) queda fuera de la regla: no hay medición.

Prueba: `tests/sql/followup_engine/validate_whatsapp_phone_equivalence.mjs` (México y Argentina en los dos sentidos, y que el correlador compartido sigue dando `conflict` con `52` contra `521`) y `tests/sql/followup_engine/real_postgres_hotmart_intent_correlation.py`.
