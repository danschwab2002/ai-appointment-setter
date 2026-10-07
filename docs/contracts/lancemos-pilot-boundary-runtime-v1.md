# Contrato V1 — wiring runtime del perímetro Lancemos

- **Estado:** Implementado en el árbol; no desplegado
- **Versión:** 1
- **Fecha:** 2026-08-10
- **Alcance:** planificación, request-start y readiness del piloto

## 1. Configuración del proceso

`LANCEMOS_PILOT_BOUNDARY_ENABLED` es `false` por defecto. Cuando vale `true`, el proceso exige al arrancar:

- `LANCEMOS_PILOT_SCOPE_KEY` no vacío;
- `LANCEMOS_PILOT_SCOPE_VERSION` entero positivo;
- `LANCEMOS_PILOT_TENANT_KEY` no vacío;
- `LANCEMOS_PILOT_CHANNEL_PROVIDER` no vacío;
- `LANCEMOS_PILOT_CHANNEL_ACCOUNT_REF` no vacío.

`RESOLUTION_WORKER_ENABLED=true` y `DURABLE_OUTBOUND_ENABLED=true` requieren el perímetro habilitado. La falta de configuración impide iniciar la aplicación; no degrada al entrypoint histórico.

## 2. Planificación

La aplicación usa `plan_lancemos_pilot_cart_recovery` para abandono de carrito.

La RPC recibe el contrato existente de planificación, la identidad Chatwoot resuelta y sólo `scope_key/version`. Tenant y routing se derivan del scope publicado, no de afirmaciones del caller. En una transacción:

1. serializa contra cambios del runtime;
2. exige scope publicado y versión activa;
3. evalúa tenant, account/inbox, proveedor/cuenta, fuente/evento, producto, oferta y cohorte (la cohorte, sólo si el `audience_mode` de la versión la usa);
4. exige que policy key/version coincidan con el scope;
5. fuera de `manual_cohort`, exige la intención con consentimiento con la que la admisión correlacionó el evento (`pilot_audience_*`);
6. sólo entonces invoca la planificación durable autoritativa.
7. vincula el caso de forma inmutable en `pilot_recovery_case_bindings` con
   `scope_key/version`, el evento admitido, `audience_mode` y, fuera de
   `manual_cohort`, `audience_purchase_intent_id` y
   `audience_precheckout_submission_id` (el envío 1.1.0 que dio el
   consentimiento).

Un rechazo usa SQLSTATE `55000`, mensaje `pilot_scope_rejected` y un `detail` reason code. La transacción no deja casos, secuencias ni acciones parciales.

El bridge copia ese rechazo a `webhook_events.processing_error` (estado `failed`) como `mensaje:detail`, o solo `mensaje` cuando el SQL no manda `detail` (por ejemplo `payment_failure_correlation_unresolved` de `plan_portable_payment_failure_recovery`). Solo lo hace para `plan_lancemos_pilot_cart_recovery` y `plan_portable_payment_failure_recovery`, y solo si el mensaje y el `detail` son tokens `snake_case`; cualquier otra falla sigue quedando como `create_recovery_case_failed`.

Los RPC históricos de planificación no tienen `EXECUTE` para roles API después de esta migración.

## 3. Request-start

La aplicación usa `mark_lancemos_pilot_request_started` inmediatamente antes del sender.

La RPC no acepta scope, tenant ni routing. Deriva el binding inmutable del caso y, desde el estado canónico, contacto, producto, oferta, account e inbox. En una transacción:

1. ejecuta `authorize_lancemos_pilot_request_start` (fuera de `manual_cohort` re-verifica la intención del binding antes de consumir presupuesto; un rechazo es `pilot_request_start_rejected` con el motivo en `detail`);
2. salvo en un replay, toma el lock de opt-out de cada forma del teléfono, las de la identidad del caso y las de `contacts.phone` (adonde sale el envío), y rechaza con `pilot_request_start_rejected` / `pilot_chatwoot_opt_out_stop` si hay un opt-out de Chatwoot de la cuenta en cualquiera de ellas, en los tres modos de audiencia. El rechazo deshace la autorización: no consume cupo (desde `20261001000100`; ver §9);
3. exige autorización actual para el mismo action/attempt;
4. compone los guards previos de autorización del contacto, compra, takeover y opt-out;
5. marca el intento como `request_started`;
6. devuelve el intento y la identidad durable de autorización.

Respuesta adicional obligatoria:

- `pilot_authorization_id: uuid`;
- `pilot_runtime_generation: bigint`;
- `pilot_authorization_replayed: boolean`.

El endpoint histórico `mark_followup_request_started` conserva su firma sólo para composición interna y falla con `pilot_request_authorization_required` si no existe autorización para el mismo action/attempt. No es ejecutable por roles API. La autorización standalone y las demás funciones internas tampoco son ejecutables por `service_role`, `anon` ni `authenticated`.

Un replay sólo es aceptable cuando el intento ya está en `request_started`. Una autorización huérfana falla con `pilot_authorization_without_request_start`.

## 4. Readiness operacional

- `GET /health`: liveness; responde `{"status":"ok"}` sin consultar dependencias.
- `GET /ready`: readiness sanitizada.

Con perímetro deshabilitado, `/ready` responde HTTP 200 y declara `default_off`.

Con perímetro habilitado, consulta `get_lancemos_pilot_runtime_status`. Un scope/version/tuple válido responde HTTP 200 incluso si el runtime está `inactive`, `paused` o `closed`: el proceso es desplegable aunque la automatización no esté armada. Configuración durable inconsistente o dependencia inaccesible responde HTTP 503.

La respuesta sólo expone:

- `status`;
- `pilot_boundary`;
- `automation_state`;
- `reason_code`.

No expone IDs de contacto, JID, teléfonos, emails, payloads, URLs ni credenciales. Los errores de dependencia se normalizan como `pilot_readiness_unavailable`.

## 5. Reason codes de readiness

- `pilot_boundary_disabled`;
- `pilot_runtime_config_invalid`;
- `pilot_scope_config_mismatch`;
- `pilot_active_scope_mismatch`;
- `pilot_runtime_inactive`;
- `pilot_runtime_armed`;
- `pilot_runtime_paused`;
- `pilot_runtime_closed`;
- `pilot_readiness_unavailable`.

## 6. Operación y compatibilidad

- La migración es aditiva, salvo el cierre explícito de los entrypoints históricos que permitían bypass.
- Con `channel_provider=waba`, outbound exige un template aprobado de primer
  contacto, idioma y categoría. Para el corte single-touch de carrito el body
  usa exactamente `{{1}} = nombre` y `{{2}} = oferta/producto`; un follow-up es
  opcional y, si no está configurado, se bloquea antes del POST. El bridge envía
  por el inbox WABA de Chatwoot usando `template_params` y nunca cae a texto libre.
- El dispatcher deriva el modo durable del provider: WABA reserva y audita
  `approved_template`; Evolution reserva y audita `freeform`.
- Request-start rechaza atómicamente `waba + freeform` y cualquier otra
  combinación provider/modo incompatible antes de crear autorización.
- La imagen y `compose.yaml` usan `/ready` como healthcheck.
- Todas las flags de efectos permanecen apagadas por defecto.
- Integrar este contrato no prueba migración aplicada, configuración remota, WABA disponible, runtime armado ni mensajes enviados.

## 7. Segundo scope: el primer contacto tras el formulario (2026-10-01, migración `20261001000200`)

La frontera del bridge sigue siendo una (`LANCEMOS_PILOT_SCOPE_KEY`, de fuente `hotmart`). El primer contacto del formulario usa **otro** scope publicado, de fuente `landing` y evento `PRECHECKOUT_FORM_SUBMITTED`, con el mismo tenant, proveedor y cuenta de canal. Un scope publicado tiene una sola fuente, así que no puede ser el mismo. Contrato del flujo: [portable-precheckout-first-contact-v1.md](portable-precheckout-first-contact-v1.md).

- **Configuración.** `LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY` y `LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION`, vacías por defecto. Las exige `PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED`, que además exige `LANCEMOS_PILOT_BOUNDARY_ENABLED` y que la clave sea distinta de `LANCEMOS_PILOT_SCOPE_KEY`.
- **Planificación.** `admit_and_plan_portable_lead_precheckout` recibe `scope_key/version`. Evalúa el scope y la audiencia con `evaluate_lancemos_pilot_scope` y `_lancemos_pilot_audience_intent`, que no se redefinen, y vincula el caso en `pilot_recovery_case_bindings` igual que §2. Un rechazo del scope no se devuelve como error: la admisión del formulario queda y el motivo se guarda en `portable_precheckout_first_contact_plans`. No acepta un scope `manual_cohort`.
- **Request-start.** El bridge elige la RPC por el `anchor_type` de la acción: `cart_abandonment` → `mark_lancemos_pilot_request_started`, `payment_failure` → `mark_portable_payment_failure_request_started`, `precheckout_intent` → `mark_portable_precheckout_request_started`. Las tres ejecutan `authorize_lancemos_pilot_request_start` y resuelven el scope por el binding inmutable del caso, no por la configuración del proceso. Después las tres miran el opt-out en las dos formas del teléfono con el lock de opt-out tomado: las del carrito y el pago fallido con `pilot_chatwoot_opt_out_stop` (§9), la del primer contacto junto con los demás frenos de su flujo. Un freno es `pilot_request_start_rejected` con el motivo en `detail` y no consume cupo. Para las tres, el cliente del bridge convierte ese rechazo en un error con su motivo y el dispatcher lo deja en el log (`durable_request_start_rejected … reason=<detail>`) y sigue con el resto del lote; el intento queda reservado y lo resuelve el lease siguiente. Cualquier otra falla del arranque sigue cortando el lote, como antes.
- **Reevaluación.** Para el ancla `precheckout_intent` el bridge llama a `reevaluate_portable_precheckout_action`, que delega en `reevaluate_followup_action` cuando nada frena. Las otras anclas siguen en `reevaluate_followup_action`.
- **Readiness.** Con el flag, `/ready` consulta `get_portable_precheckout_pilot_runtime_status` y suma `portable_precheckout_first_contact` con el estado del runtime de ese scope (`inactive`, `armed`, `paused`, `closed`). Un scope mal configurado responde `503` con `portable_precheckout_` más el motivo de §5 (`pilot_runtime_config_invalid`, `pilot_scope_config_mismatch`, `pilot_active_scope_mismatch`); una dependencia inaccesible, `portable_precheckout_readiness_unavailable`. Esa función es una copia de `get_lancemos_pilot_runtime_status` para la fuente `landing`, y además informa `pilot_scope_config_mismatch` para un scope `manual_cohort`. Sin el flag, `/ready` es el de §4.
- **Topes.** Cada scope tiene sus topes y su cohorte. El total de envíos de un scope cuenta lo consumido por todas sus versiones.
- **Tope por persona entre scopes** (desde `20261007000100`, apagado por defecto). Si el tenant del scope tiene renglón en `pilot_proactive_contact_caps`, `authorize_lancemos_pilot_request_start` cuenta los arranques autorizados de la persona en todos los scopes del tenant dentro de `request_window`. La persona es el contacto y cualquier otro contacto de la misma cuenta con una identidad de WhatsApp en alguna de las dos formas del teléfono.
  - Con `max_request_starts` o más, devuelve `authorized = false` con `pilot_contact_proactive_cap_reached`: sin renglón en el ledger, sin evento y sin consumir el cupo del scope.
  - Corre después del replay, la cohorte y la audiencia, y antes de los topes del scope.
  - Toma `pg_advisory_xact_lock` por tenant y teléfono canónico, después del lock del control y antes de los locks de opt-out del envoltorio.
  - Sin renglón, la función es la de antes.

## 8. Lectura del modo de audiencia (2026-10-01, migración `20261001000300`)

```text
public.get_lancemos_pilot_scope_audience_mode(p_scope_key text, p_scope_version integer) returns text
```

Devuelve el `audience_mode` de una versión **publicada**, o `null` si no existe o no está publicada. Es `security definer`, estable y ejecutable solo por `service_role`; no reemplaza ninguna función y no toma locks sobre tablas calientes. Existe porque `pilot_scope_versions` tiene RLS y ningún rol de la API la lee.

La usa una sola guarda: con `[adaptadores.ghl]` en el manifiesto, sin la aceptación escrita del riesgo y con la frontera prendida, el bridge lee el modo del scope de `LANCEMOS_PILOT_SCOPE_KEY` en el arranque (antes de levantar cualquier worker) y en `/ready`, y solo sigue con `manual_cohort`. Motivos, en el arranque y como `detail` del `503` de `/ready`:

- `ghl_adapter_risk_not_accepted`: el scope es `consented_intent` o `consented_intent_in_cohort`;
- `ghl_adapter_risk_audience_unavailable`: la lectura falla, devuelve `null`, devuelve un modo desconocido o el bridge no tiene Supabase.

En `/ready` va después del chequeo del scope de §4: un scope sin configurar responde su motivo de siempre. Sin la sección, con la aceptación o con la frontera apagada, la función no se llama. Contrato: [ghl-precheckout-adapter-v1.md](ghl-precheckout-adapter-v1.md), *La aceptación escrita del riesgo*.

## 9. Teléfonos en dos formas: dos motivos más de la audiencia y el opt-out al arrancar (2026-10-01, migración `20261001000100`)

En `consented_intent` y `consented_intent_in_cohort`, la evidencia que se exige al planificar y se vuelve a verificar al arrancar suma dos chequeos, para carrito y pago fallido:

- `pilot_audience_consented_intent_contact_phone_mismatch`: el teléfono del contacto, que es adonde sale el envío, no es el teléfono consentido;
- `pilot_audience_consented_intent_prior_opt_out`: hay un opt-out de Chatwoot de la cuenta del binding en cualquiera de las dos formas del teléfono.

Las comparaciones de teléfono de esa evidencia pasan a ser en forma canónica (`52…` ≡ `521…`, `54…` ≡ `549…`). En `manual_cohort` la evidencia no cambia.

Además, en los tres modos, `mark_lancemos_pilot_request_started` (carrito) y `mark_portable_payment_failure_request_started` miran el opt-out justo antes de arrancar (paso 2 de §3), con `_portable_chatwoot_opt_out_stop`: toma en orden el lock de opt-out de cada forma de la identidad y de `contacts.phone`, el mismo de `apply_chatwoot_inbound_opt_out` y `mark_followup_request_started`, y mira los mismos estados que este último (`applied`, `unmatched`, `ambiguous`, `evidence_conflict`) en la cuenta de la identidad. El rechazo es `pilot_request_start_rejected` / `pilot_chatwoot_opt_out_stop`, que el bridge trata como cualquier rechazo del piloto (§7). Existe por `manual_cohort`: ahí la evidencia de arriba no corre y el freno compartido busca el id exacto de la identidad, así que un «No más mensajes» guardado `unmatched` bajo `521…` no frenaba el carrito de una identidad `52…`, ni el pago fallido que usa el permiso que ese carrito concedió (los dos comparten el propósito `cart_recovery`), y el envío salía a ese mismo `wa_id`.

Límite conocido: con un opt-out `unmatched` la reevaluación no lo ve y sigue ejecutando, así que el intento queda reservado y el arranque lo rechaza en cada lease hasta que la acción vence, como pasa hoy con el `pending_chatwoot_opt_out_stop` del freno compartido. El permiso `allowed` que concedió el carrito sigue vivo, pero no tiene efecto: los dos arranques lo frenan.

Detalle: [commercial-ally-runtime-v1.md](commercial-ally-runtime-v1.md#equivalencia-de-teléfonos-de-whatsapp-2026-10-01-migración-20261001000100).
