# Perímetro acotado del piloto de Lancemos

- **Estado:** Fase 1 implementada y verificada localmente; wiring de fase 2 pendiente
- **Fecha:** 2026-08-10
- **Alcance:** MVP V1, un tenant, un inbox, un número/cuenta de canal, un producto, una oferta, cohorte explícita, presupuesto de requests y kill switch
- **No implica:** activación de outbound, carga de valores reales, despliegue de DDL, configuración WABA ni contacto con leads reales
- **Fuente de producto:** [Dirección del piloto](./lancemos-pilot-product-direction.md)
- **Contrato técnico:** [Perímetro Lancemos V1](../contracts/lancemos-pilot-boundary-v1.md)

## 1. Problema

La allowlist de un único JID protege pruebas controladas, pero no constituye el perímetro de una cohorte real. Tampoco es seguro pasar al piloto eliminándola. El reemplazo debe ser una conjunción autoritativa y durable:

```text
tenant Lancemos
AND scope publicado
AND runtime armado
AND cuenta + inbox canónicos
AND cuenta/número de canal esperado
AND evento Hotmart de abandono permitido
AND producto + oferta exactos
AND autorización del contacto vigente
AND contacto dentro de la audiencia de la versión (cohorte y/o intención consentida, según audience_mode)
AND presupuesto total y diario disponible
AND stops negativos ausentes
→ recién entonces puede comenzar un request outbound
```

Las restricciones negativas existentes —opt-out, compra, takeover, delivery incierto y conflicto de correlación— conservan precedencia. El perímetro no reemplaza esos gates: agrega otro requisito obligatorio.

## 2. Decisiones de fase 1

### 2.1 Scope versionado e inmutable

Cada configuración se identifica por `scope_key + version`. Una versión publicada fija:

- tenant `lancemos`;
- cuenta e inbox de Chatwoot;
- proveedor y referencia opaca de la cuenta/número del canal;
- source/evento `hotmart/PURCHASE_OUT_OF_SHOPPING_CART`;
- producto y oferta exactos;
- policy key/version de seguimiento;
- audiencia (`audience_mode`, §2.4);
- timezone;
- máximo de contactos activos;
- máximo total de request-starts;
- máximo diario de request-starts.

Una versión publicada es inmutable. Cambiar cualquier límite o identificador exige otra versión y una activación explícita. La activación sólo se admite desde `inactive|paused`, fuerza la nueva versión a `inactive`, incrementa la generación y no migra miembros de cohorte. El operador debe revisar la nueva versión, inscribir su cohorte y armarla en pasos separados.

### 2.2 Default apagado

Crear o publicar configuración nunca activa outbound. El control runtime comienza `inactive`. Sólo un RPC administrativo puede moverlo a `armed`, con actor, motivo y compare-and-swap por generación.

### 2.3 Kill switch fail-closed

El estado `paused` o `closed` bloquea nuevos request-starts. El RPC de cambio de estado y la reserva del presupuesto bloquean la misma fila de control. Por lo tanto, quedan serializados:

- si la pausa confirma primero, la reserva posterior se rechaza;
- si una reserva confirma primero, ese request ya cruzó honestamente la frontera durable y la pausa impide los siguientes.

No se promete cancelar una llamada externa que ya empezó.

### 2.4 Audiencia del scope: cohorte explícita o intención consentida

Cada versión publicada fija su `audience_mode`, que decide qué contactos pueden entrar al piloto. Es parte de la versión: cambiarlo exige publicar otra versión y activarla (§2.1), nunca editar la publicada. La migración `20260930000300` lo agrega; todos los scopes anteriores quedan en `manual_cohort`.

| modo | cohorte | intención consentida | uso |
|---|---|---|---|
| `manual_cohort` (default) | la exige | no la mira | el de siempre; Johanna y la v1 de ATT1 |
| `consented_intent_in_cohort` | la exige | la exige | E2E controlado con un solo teléfono |
| `consented_intent` | no la mira | la exige | producción |

- **`manual_cohort`.** El abandono no incorpora automáticamente un contacto a la cohorte. Un operador autorizado debe inscribirlo mediante RPC. La inscripción usa sólo `contact_id`, nunca PII; respeta el máximo de contactos activos; es idempotente; puede retirarse sin borrar auditoría; no equivale por sí sola a consentimiento ni permiso de envío.
- **`consented_intent_in_cohort`.** Exige las dos cosas: estar inscripto y tener evidencia de intención consentida. Con `max_cohort_contacts = 1` y el teléfono de prueba inscripto se ejercita la misma evidencia que usará producción sin que un lead real pueda entrar. Hace falta: con `consented_intent` y un tope total de 1, el tope limita a un envío pero no a un teléfono, y el primer lead real que abandone se lo lleva.
- **`consented_intent`.** La cohorte no se consulta. Entra un contacto cuyo evento quedó atado a una intención de compra que:
  - es la que la admisión correlacionó con **ese** evento: `hotmart_purchase_intent_correlations.outcome = 'resolved'` para el carrito, `commercial_ally_payment_failure_details.correlation_outcome = 'resolved'` para el pago fallido;
  - es del tenant, el producto y una oferta del scope, de **la misma oferta del evento**, con su mapeo activo en `hotmart_purchase_intent_scopes`;
  - cumple el criterio de Johanna sin sus valores fijos (`_portable_consented_intent_reason`, migración `20260930000100`): sigue viva (`waiting_for_purchase`, observada por el proveedor, no provisional, sin `identity_conflict`, `tracking_incomplete` ni `expired_unknown`), tiene `whatsapp_contact_authorized` y `activation_authorized`, su teléfono es el de destino y un `contact_point` del contacto, y tiene un envío 1.1.0 del formulario con `whatsapp_contact` y `marketing_optin` en `true`, la `consent_copy_version` del binding activo y ningún conflicto abierto.

  Los modos con consentimiento se admiten en scopes con `source` `hotmart` o `landing` (el primer contacto del formulario los va a usar).

La evidencia se verifica en dos momentos, siempre atada al evento y nunca buscada por contacto (una intención de otra oferta, otro producto u otro teléfono no cuenta aunque sea de la misma persona):

1. **Al planificar.** `plan_lancemos_pilot_cart_recovery` y `plan_portable_payment_failure_recovery` la exigen antes de crear el caso y registran en `pilot_recovery_case_bindings` el modo, el `purchase_intent_id` y el envío 1.1.0 del formulario que dio el consentimiento (`audience_precheckout_submission_id`; su `canonical_payload` guarda la `copy_version`). Así se puede reconstruir con qué evidencia exacta entró cada caso, aunque después llegue otro formulario. Un rechazo es `pilot_scope_rejected` con el motivo en `detail`, no deja caso ni permiso, y el bridge lo guarda en `webhook_events.processing_error` con estado `failed`.
2. **Al arrancar el envío.** `authorize_lancemos_pilot_request_start` vuelve a verificar la intención del binding contra la identidad seleccionada **antes de consumir presupuesto**, y registra en `pilot_outbound_request_authorized` el modo, la intención y el envío que dio el consentimiento en ese momento. Si entre el plan y el envío la intención se compró (la correlación de la compra aprobada), pasó a `identity_conflict` (otro formulario de la misma oferta con otro teléfono o email), salió del mapeo activo o perdió el envío que la sostenía, el request no arranca, devuelve el motivo `pilot_audience_*` y no consume cupo. Lo que serializa el lock compartido es solo la fila de la intención: espera a quien la esté cambiando (la compra, un formulario posterior) y lee la versión confirmada. Lo que no toca esa fila (un conflicto abierto del envío, la `copy_version` o el estado del binding comercial, el mapeo de la oferta) no queda serializado: se ve si ya estaba confirmado al leerlo. Las pruebas corren en PGlite, que tiene una sola conexión: cubren cada uno de esos cambios hecho antes del arranque, no una carrera entre dos transacciones. El replay de una autorización ya cruzada no re-chequea (probado: con el consentimiento perdido, el mismo intento vuelve a arrancar con `replayed = true`).

   La compra la corta primero el worker de compras, que cierra el caso; la re-verificación cubre el envío que ya estaba en vuelo cuando entró la compra.

La evaluación temprana (`evaluate_lancemos_pilot_scope`) sólo mira la cohorte en los modos que la usan. En `consented_intent` devuelve `pilot_scope_allowed` sin mirar la intención, porque sus llamadores (los planificadores) la atan al evento en la misma transacción; como siempre, una evaluación positiva no es un permiso de envío (§2.6). Sola no sirve como filtro de audiencia, y dos pruebas lo frenan: `validate_pilot_scope_audience_mode.mjs` exige que toda función SQL que la llame llame también a `_lancemos_pilot_audience_intent` (un llamador nuevo, como el primer contacto del formulario, rompe ahí si se olvida), y `test_pilot_scope_audience_mode_migration.py` exige que ni el bridge ni los scripts la llamen.

En `consented_intent` el freno son los topes total y diario (§2.5); `max_cohort_contacts` sigue siendo obligatorio pero no se consulta, e inscribir miembros no tiene efecto. Ningún modo equivale por sí solo a permiso de envío: opt-out, compra, takeover, delivery incierto y conflicto de correlación conservan precedencia (§1). La audiencia no mira el opt-out; lo frenan las piezas de siempre, y en `consented_intent` está probado sin cohorte: con la baja previa al evento el caso se planifica, pero no se concede ningún permiso `allowed` y la reevaluación lo cancela (`contact_blocked`); con la baja entre el plan y el envío la acción queda cancelada, o, si el intento ya estaba reservado, el arranque se rechaza. En ningún caso se consume cupo.

Pasar del E2E a producción: pausar, publicar la versión siguiente con `consented_intent` y los topes aprobados (el total cuenta lo consumido por todas las versiones), activarla (queda `inactive`), apuntar `LANCEMOS_PILOT_SCOPE_VERSION` a la versión nueva, reiniciar y armar. Los casos planificados con la versión anterior ya no se envían (`pilot_scope_version_mismatch`).

Rotar la `consent_copy_version` del binding comercial con casos pendientes de un scope con consentimiento los deja trabados. La re-verificación del arranque compara el envío del formulario con la copy del binding activo **en ese momento**, no con la que regía al planificar: un caso planificado con la copy vieja se rechaza para siempre con `pilot_audience_consented_intent_submission_missing`. Además, el dispatcher no distingue ese rechazo de otras fallas del arranque: registra el error y la acción no avanza ni se cierra sola. Por eso la copy no se rota mientras un scope con consentimiento tenga acciones pendientes: primero se dejan salir o vencer (o se cierran), y recién después se rota. La alternativa, comparar contra la copy que regía al planificar (el envío ya está en el binding), queda como decisión abierta.

Riesgos que quedan: si el carrito de Hotmart llega antes que el formulario, la correlación queda `unmatched` y no se recalcula, así que ese carrito no entra; y `resolve_event` crea el contacto también para compradores fuera de la audiencia (ya pasaba, pero el volumen crece).

### 2.5 Presupuesto conservador

El presupuesto cuenta autorizaciones durables de request-start, no mensajes confirmados. En fase 2 la autorización debe acoplarse con la transición durable `request_started` inmediatamente antes del efecto. Es deliberadamente conservador: una autorización consumida no devuelve cupo automáticamente, porque el efecto pudo ocurrir. Una reserva se identifica por `attempt_id`, por lo que el replay del mismo intento no consume dos veces.

Hay dos caps obligatorios:

- total de requests outbound del piloto;
- requests outbound por fecha local del scope.

Los consumos se cuentan por `scope_key` a través de todas sus versiones. Publicar o activar una versión nueva no reinicia presupuesto.

La timezone es invariante para todas las versiones de un mismo `scope_key`. Cambiarla durante el piloto podría partir artificialmente el día presupuestario; por eso requiere cerrar el scope y una decisión operativa explícita, no una activación V1→V2.

El cap total forma parte del cierre conservador de este diseño. La política comercial concreta del cap diario todavía requiere confirmación de Juan/operación; la implementación lo exige igualmente como guarda fail-closed y no permite armar un scope real hasta cargar un valor aprobado. Esto no declara que el valor o la política diaria ya hayan sido aceptados como requisito de producto.

### 2.6 Dos fronteras

1. **Evaluación:** permite rechazar temprano admisión/planificación fuera de scope, sin consumir presupuesto.
2. **Autorización de request-start:** revalida todo, exige la audiencia de la versión (§2.4) y consume presupuesto atómicamente inmediatamente antes del efecto.

Sólo la segunda habilita un efecto. Una evaluación positiva anterior no es un permiso durable para enviar después.

## 3. Modelo propuesto

### `pilot_scope_versions`

Configuración publicada e inmutable del scope.

### `pilot_runtime_controls`

Estado mutable `inactive|armed|paused|closed`, versión activa y generación CAS. Una fila por `scope_key`.

### `pilot_cohort_memberships`

Membresía auditable por contacto y versión, con estado `active|removed`.

### `pilot_recovery_case_bindings`

Vínculo inmutable de cada caso con `scope_key/version` y el evento admitido. Registra además `audience_mode` y `audience_purchase_intent_id`: con qué evidencia entró el caso (`manual_cohort` sin intención; los otros dos modos, con la intención).

### `pilot_outbound_request_authorizations`

Ledger append-only de slots consumidos por `attempt_id`. Conserva action/contact, fecha local, versión y generación observada.

### `pilot_control_events`

Auditoría append-only de activación, pausa, cierre e inscripción/retiro. En `pilot_outbound_request_authorized`, fuera de `manual_cohort`, `data` suma `audience_mode` y `audience_purchase_intent_id`; en `manual_cohort` queda igual que antes (`local_budget_date`).

## 4. Reason codes mínimos

Evaluación y autorización devuelven un resultado tipado. Entre otros:

- `pilot_scope_allowed`;
- `pilot_scope_not_published`;
- `pilot_runtime_not_armed`;
- `pilot_scope_version_mismatch`;
- `pilot_tenant_mismatch`;
- `pilot_chatwoot_account_mismatch`;
- `pilot_chatwoot_inbox_mismatch`;
- `pilot_channel_account_mismatch`;
- `pilot_source_event_mismatch`;
- `pilot_product_mismatch`;
- `pilot_offer_mismatch`;
- `pilot_contact_not_in_cohort`;
- `pilot_audience_intent_unresolved` (el evento no quedó correlacionado con una intención);
- `pilot_audience_intent_scope_mismatch` (la intención no es del tenant, producto u oferta del evento y del scope, o su mapeo no está activo);
- `pilot_audience_consented_intent_*`, el motivo de `_portable_consented_intent_reason` con el prefijo `pilot_audience_`: `not_found`, `not_live`, `not_authorized`, `phone_mismatch`, `binding_unavailable`, `submission_missing`, `input_invalid`;
- `pilot_audience_input_invalid`;
- `pilot_total_budget_exhausted`;
- `pilot_daily_budget_exhausted`;
- `pilot_attempt_mismatch`;
- `pilot_request_time_invalid`.

Valores nulos, vacíos o malformados fallan cerrados; no se interpretan como comodines.

## 5. Integración por fases

### Fase 1 — este workstream

- tablas, constraints, índices y ACL;
- control runtime CAS;
- cohort enrollment/removal;
- evaluación tipada;
- autorización idempotente de request-start;
- pruebas SQL adversariales;
- contrato y diseño.

No modifica todavía los entrypoints centrales que el workstream de abandono puede estar auditando.

### Fase 2 — después de integrar el workstream de abandono

- llamar la evaluación en admisión/planificación;
- propagar scope key/version al caso y la acción;
- llamar la autorización en la frontera durable `request_started`;
- sumar settings/deployment contract;
- prueba HTTP controlada; la concurrencia SQL ya fue comprobada localmente en PostgreSQL real durante fase 1 y debe revalidarse después del wiring.

## 6. Invariantes

1. No hay wildcard para tenant, inbox, producto, oferta ni cuenta de canal.
2. Publicar no arma el piloto.
3. Sólo una versión puede estar seleccionada por el control runtime.
4. Cambiar versión exige pausa/inactividad y siempre deja el runtime `inactive`.
5. `paused|closed|inactive` nunca autorizan un request nuevo.
6. Un contacto fuera de la audiencia de su versión nunca consume presupuesto ni envía.
7. El máximo de cohorte se aplica bajo el mismo lock del control (en los modos que usan la cohorte).
8. Los caps total/diario se verifican y consumen en una transacción y no se reinician al cambiar versión.
9. El replay del mismo `attempt_id` devuelve el resultado original sin consumir otro slot.
10. Otro `attempt_id` no puede reutilizar la misma acción de forma incompatible.
11. Pausa y request-start quedan serializados; no se promete deshacer efectos ya iniciados.
12. Tablas autoritativas no permiten DML directo a roles API ni a `service_role`.
13. RPCs mutantes son `SECURITY DEFINER`, con `search_path` cerrado y sólo `service_role`.
14. No se almacenan teléfonos, JIDs, nombres, mensajes ni payloads en las tablas del perímetro.

## 7. Temas externos pendientes

Antes de crear una versión publicada real deben confirmarse:

- account e inbox definitivos de Lancemos;
- proveedor WABA y referencia opaca del número/cuenta;
- producto y offer code exactos;
- policy version aprobada;
- timezone operativa;
- tamaño máximo de cohorte;
- cap total y diario;
- operador habilitado para armar/pausar;
- duración/fecha de cierre del piloto.

Ninguno de esos valores se inventa en la migración.
