# Contrato: primer contacto portable tras el formulario (v1)

- **Estado:** implementado en el árbol (bridge 1.3.0, sin publicar); apagado por defecto; sin E2E real. No describe nada desplegado ni activado.
- **Fecha:** 2026-10-01
- **Alcance:** una instancia con manifiesto v2 y salida por WABA. El envío del formulario de la landing (`lead.precheckout` 1.1.0) con consentimiento de WhatsApp planifica un único primer contacto, demorado, que el dispatcher durable manda con la plantilla aprobada de `[plantillas.precheckout]`.
- **Migraciones:** `20261001000100_whatsapp_phone_equivalence.sql` (la comparación de teléfonos que usa) y `20261001000200_portable_precheckout_first_contact.sql` (el flujo).
- **Relacionados:** [lead-precheckout-v1.md](lead-precheckout-v1.md), [ghl-precheckout-adapter-v1.md](ghl-precheckout-adapter-v1.md), [lancemos-pilot-boundary-runtime-v1.md](lancemos-pilot-boundary-runtime-v1.md), [approved-template-direct-dispatch-v1.md](approved-template-direct-dispatch-v1.md), [commercial-ally-runtime-v1.md](commercial-ally-runtime-v1.md), [instance-runtime-v2.md](instance-runtime-v2.md).

## Propósito y límite

Hasta esta versión el formulario de una instancia portable solo dejaba la intención de compra. Los dos planificadores del piloto arrancan de un evento de Hotmart (carrito o pago fallido), así que quien no llegaba al checkout no recibía nada. Este contrato suma el tercer disparador.

- Un envío nuevo del formulario, con los dos permisos, planifica **un** primer contacto: la acción `first_contact_review` con ancla `precheckout_intent`, sobre un caso de fuente `landing`.
- Sale después de la demora de la política del scope, contada desde el envío que disparó el plan.
- Se cancela si antes de salir la persona compra, llega su carrito o su pago fallido, se da de baja, la derivan a una persona del equipo o pierde el consentimiento. El caso queda `cancelled`, nunca `won`: la venta de alguien a quien no se le escribió no es de este flujo.
- No manda seguimientos. La política de este flujo tiene un solo paso (`first_contact`).

Johanna corre sin manifiesto: el flag no arranca sin él, su `/webhooks/lead` sigue en `admit_observed_lead_precheckout` y ninguna función que ejecuta se redefine.

## Configuración

| Variable | Por defecto | Qué es |
|---|---|---|
| `PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED` | `false` | Prende el flujo: la admisión del formulario planifica y el dispatcher manda |
| `LANCEMOS_PILOT_PRECHECKOUT_SCOPE_KEY` | vacía | El scope del piloto de este flujo |
| `LANCEMOS_PILOT_PRECHECKOUT_SCOPE_VERSION` | vacía | Su versión publicada, entero positivo |
| `WABA_PRECHECKOUT_TEMPLATE_NAME` | vacía | La plantilla del flujo. Tiene que ser `plantillas.precheckout.nombre`. Sin manifiesto, definirla impide arrancar |

Con el flag prendido el bridge no arranca si falta alguna de estas condiciones:

- manifiesto v2, con `flujos.precheckout` en `true` (y por él, el evento `intencion` y `[plantillas.precheckout]`);
- una entrada del formulario: `LEAD_PRECHECKOUT_ENABLED` o `GHL_PRECHECKOUT_ADAPTER_ENABLED`;
- `LANCEMOS_PILOT_BOUNDARY_ENABLED`, `DURABLE_DISPATCHER_ENABLED` y `DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED` (que a su vez exige `DURABLE_OUTBOUND_ENABLED`, el proveedor `waba` y las variables `WABA_*` de la salida: `WABA_FIRST_TOUCH_TEMPLATE_NAME`, `WABA_TEMPLATE_LANGUAGE` y `WABA_TEMPLATE_CATEGORY` se exigen aunque este sea el único flujo prendido);
- el scope y su versión, y que la clave sea **otra** que `LANCEMOS_PILOT_SCOPE_KEY`: un scope publicado tiene una sola fuente, y el de recuperación es de fuente `hotmart`;
- `WABA_PRECHECKOUT_TEMPLATE_NAME` igual a `plantillas.precheckout.nombre`, con `WABA_TEMPLATE_LANGUAGE` igual a su `idioma`;
- lo que tiene que poder frenarlo: `PORTABLE_HOTMART_PURCHASE_STOP_ENABLED` con `HOTMART_HOTTOK` (sin el hottok el webhook de Hotmart responde `503` y ninguna compra frena nada) y `CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED`, que arrastra el opt-out durable y la derivación;
- con `[adaptadores.ghl]` en el manifiesto, la aceptación escrita del riesgo del adaptador ([ghl-precheckout-adapter-v1.md](ghl-precheckout-adapter-v1.md#riesgos)): sin ella `flujos.precheckout` no arranca.

Apagado, nada de eso se evalúa. Además, la plantilla del flujo entra a la configuración del dispatcher solo con el flag: apagado, una acción `precheckout_intent` que hubiera quedado planificada cierra `permanent_failed` en el primer intento y no vuelve a salir al prenderlo.

### El scope y la política

El scope del flujo se publica en la base de la instancia, igual que el de recuperación:

- `source = 'landing'` y `source_event_type = 'PRECHECKOUT_FORM_SUBMITTED'`;
- `audience_mode` `consented_intent` o `consented_intent_in_cohort`. Un scope `manual_cohort` no sirve para este flujo: el planificador lo rechaza y el status lo informa como `pilot_scope_config_mismatch`;
- el mismo tenant, proveedor y cuenta de canal que el scope de recuperación (el bridge los toma de `LANCEMOS_PILOT_TENANT_KEY`, `LANCEMOS_PILOT_CHANNEL_PROVIDER` y `LANCEMOS_PILOT_CHANNEL_ACCOUNT_REF`);
- la cuenta, el inbox y el producto del binding activo, y la oferta de la intención entre `offer_code` y `additional_offer_codes`;
- una política publicada de propósito `cart_recovery` (el único que admite el scope) con el paso `first_contact`. Su `grace_period` es la demora y su `expires_after` el vencimiento.

El fixture `tests/fixtures/instances/att1/politica-piloto.json` (sección `first_contact`) trae el scope y la política con que se prueba: demora de 60 minutos, vencimiento de 1 día, un mensaje.

## Admisión y plan

Con el flag, `/webhooks/lead` (con manifiesto) y el adaptador de GHL llaman a una sola RPC en lugar de `admit_portable_observed_lead_precheckout`:

```text
public.admit_and_plan_portable_lead_precheckout(
  p_tenant_ref text,
  p_funnel_ref text,
  p_binding_version integer,
  p_external_submission_id text,
  p_raw_payload jsonb,
  p_canonical_payload jsonb,
  p_scope_key text,
  p_scope_version integer
) returns table (
  outcome text,             -- inserted | duplicate | semantic_conflict
  submission_id uuid,
  purchase_intent_id uuid,
  plan_outcome text,        -- planned | not_planned | plan_failed | null
  plan_reason text
)
```

- Sin scope o con versión inválida falla con `22023 invalid_pilot_plan_parameters` antes de admitir.
- Bloquea el control del scope y después llama a `admit_portable_observed_lead_precheckout`, que no cambia. El orden control → intención es el de la autorización del envío.
- Solo un envío `inserted` planifica. Con `duplicate` o `semantic_conflict` devuelve el plan ya guardado de ese envío, o nulos.
- **La admisión no se pierde por el plan.** Todo lo que sigue corre en un bloque que atrapa cualquier error y deja el motivo en el renglón del plan. Los triggers diferidos de la acción y del caso nuevos se disparan adentro del bloque (`set constraints all immediate`), así su error tampoco tumba la admisión en el commit. Solo se relanzan los errores transitorios (clases `40`, `53`, `57` y `08`, y `55P03`): ahí se deshace también la admisión y el emisor reintenta el envío entero.
- **La respuesta HTTP no cambia.** `/webhooks/lead` y el adaptador responden lo mismo con el flag prendido o apagado: el resultado del plan no viaja al emisor.

### En qué orden decide

Antes de crear nada:

1. **La intención y el envío.** La intención viva (`waiting_for_purchase`, observada, no provisional, con teléfono, sin `identity_conflict`, `tracking_incomplete` ni `expired_unknown`), con `whatsapp_contact_authorized` y `activation_authorized`; y el envío que dispara, 1.1.0 y con `consent.whatsapp_contact` y `consent.marketing_optin` en `true`. Un envío sin opt-in sobre una intención que ya consintió no planifica.
2. **El scope, sin contacto.** Publicado, con el control en esa versión y `armed`, de fuente `landing`, no `manual_cohort`, y con tenant, cuenta, inbox, producto y oferta del binding; la política publicada con el paso `first_contact`; y el envío todavía sin vencer.
3. **El contacto, sin crearlo.** Se busca por punto de email, por punto de teléfono (las dos formas del número), por identidad de WhatsApp de la cuenta (las dos formas) y por el email del contacto. Dos dueños distintos no se planifican. En `consented_intent_in_cohort` el contacto tiene que existir e integrar la cohorte activa.
4. **Los frenos** de [Qué lo frena](#qué-lo-frena).

Recién entonces:

5. **El contacto y el plan.** Si el contacto no existe se crea; al que existe se le completan `phone`, `full_name` y `email` nulos (el que nació de un mensaje entrante no tiene ninguno) y los puntos de contacto, con fuente `system`. El planificador vuelve a evaluar el scope y la audiencia con las funciones del piloto (`evaluate_lancemos_pilot_scope` y `_lancemos_pilot_audience_intent`, que no se redefinen), y crea:
   - el ancla: un `webhook_events` de fuente `system`, `external_event_id = 'precheckout-submission:<submission_id>'`, que nace `processed` y con un `payload` que lleva solo ids y la fecha del envío;
   - el caso (`source = 'landing'`, `context.trigger_kind = 'precheckout_intent'`), su secuencia (`reason = 'precheckout_intent'`) y la acción `first_contact_review` con `anchor_type = 'precheckout_intent'`;
   - `due_at = submitted_at + grace_period` y `expires_at = submitted_at + expires_after`, con el `submitted_at` **del envío que dispara**, no el de la intención (que puede ser de un envío anterior);
   - la identidad de WhatsApp: reutiliza la identidad activa del contacto en cualquiera de las dos formas del teléfono (primero la exacta); si no hay, la crea con el teléfono de la intención;
   - el permiso `contact_authorizations` `allowed` de fuente `system` (evidencia `reason = precheckout_whatsapp_consent`), solo si no hay una fila activa. Una fila activa de cualquier estado gana;
   - el vínculo del caso con el scope en `pilot_recovery_case_bindings`, con la evidencia de audiencia.

### El renglón del plan

`portable_precheckout_first_contact_plans` guarda un renglón por envío admitido con el flujo prendido: `submission_id`, `purchase_intent_id`, `contact_id`, `recovery_case_id`, `scope_key`, `scope_version`, `outcome`, `reason_code`, `error_sqlstate` y `created_at`. Tiene RLS y ningún rol de la API la lee ni la escribe, `service_role` incluido.

| `outcome` | Qué pasó | `reason_code` |
|---|---|---|
| `planned` | Hay caso y acción | `first_contact_scheduled`; si una compra ya conocida cerró la acción al nacer, el motivo terminal de la acción (por ejemplo `purchase_detected`) |
| `not_planned` | Se decidió no planificar | Un motivo de la tabla de abajo |
| `plan_failed` | El plan dio un error que no es un rechazo | El mensaje del error si tiene forma de código (`^[a-z0-9_]{1,64}$`), si no `plan_error_unclassified`; el SQLSTATE va en `error_sqlstate` |

Un texto libre de la base puede traer datos de la persona, así que nunca se guarda ni se devuelve.

Motivos de `not_planned`:

| Motivo | Cuándo |
|---|---|
| `intent_purchased` | La intención ya está `purchased` |
| `precheckout_intent_not_live` | La intención no está viva |
| `precheckout_intent_not_authorized` | Le falta alguno de los dos permisos |
| `precheckout_submission_not_consented` | El envío que dispara no es 1.1.0 o no trae el opt-in |
| `precheckout_binding_unavailable` | No hay binding activo de la intención |
| `pilot_scope_not_published`, `pilot_scope_version_mismatch`, `pilot_runtime_not_armed` | El scope no está publicado, el control está en otra versión, o no está `armed` |
| `pilot_source_event_mismatch`, `precheckout_scope_audience_unsupported` | El scope es de otra fuente, o es `manual_cohort` |
| `pilot_tenant_mismatch`, `pilot_chatwoot_account_mismatch`, `pilot_chatwoot_inbox_mismatch`, `pilot_product_mismatch`, `pilot_offer_mismatch` | El scope no es el del binding o no cubre la oferta |
| `precheckout_policy_unavailable` | La política no está publicada o no tiene el paso `first_contact` |
| `precheckout_submission_expired` | El envío ya pasó su `expires_after` |
| `precheckout_contact_input_invalid`, `precheckout_contact_ambiguous` | La intención no tiene email o teléfono, o la identidad cae en dos contactos |
| `pilot_contact_not_in_cohort` | En `consented_intent_in_cohort`, el contacto no existe o no integra la cohorte |
| `intent_purchase_ambiguous`, `purchase_by_identity`, `superseded_by_provider_event`, `precheckout_prior_opt_out`, `precheckout_conversation_handoff` | Un freno |
| `precheckout_contact_already_planned` | Ya hay un primer contacto vivo de esa persona |
| `pilot_audience_*` y los demás `detail` de `pilot_scope_rejected` | Un rechazo del scope o de la audiencia al planificar (por ejemplo, el teléfono del contacto no es el consentido) |

Un `plan_failed` o un `not_planned` es definitivo para ese envío: solo un envío nuevo del formulario vuelve a intentar.

## Qué lo frena

`_portable_precheckout_stop_reason(intención, contacto)` es una sola función privada, de lectura, que comparten el planificador, la reevaluación y el arranque del envío. Devuelve el primer motivo que aplica:

| Motivo | Condición |
|---|---|
| `intent_purchased` | La intención está `purchased` |
| `precheckout_intent_not_live` | La intención dejó de estar viva |
| `intent_purchase_ambiguous` | Hay una compra aprobada que no se pudo atribuir a una sola intención y esta es candidata (`portable_hotmart_purchase_correlation_candidates`) |
| `purchase_by_identity` | Hay una compra aprobada admitida del binding con el mismo email, o con el mismo teléfono en cualquiera de sus dos formas, de la intención o de un punto del contacto. **No tiene ventana:** no depende del `max_lookback` de la correlación |
| `superseded_by_provider_event` | La intención quedó clasificada `confirmed_abandonment` o `payment_failure_supported`, o el contacto tiene un caso de fuente `hotmart` abierto del producto. Ese flujo le escribe; este no |
| `precheckout_prior_opt_out` | Hay un opt-out de Chatwoot de la cuenta del binding en cualquiera de las dos formas del teléfono |
| `precheckout_conversation_handoff` | El contacto tiene una conversación derivada a una persona (`human_takeover`), en `paused_human`, `closed` o `blocked`, o con la automatización en `paused`, `disabled`, `restricted` o `error` |

El último es el criterio `blocked_handoff` del primer toque de Johanna (`20260829000300` al reservar y `20260829000400` al arrancar), copiado tal cual. Hace falta acá porque la derivación del entrante marca la **conversación** del contacto, no el caso de fuente `landing` (que nace sin conversación): la reevaluación compartida sola no la vería. Mira el estado de la conversación en ese momento; no guarda historia. Va al final porque un opt-out aplicado también deja la conversación bloqueada y conserva su propio motivo.

Dos frenos más viven fuera de esa función:

- **Un primer contacto vivo por persona.** No se planifica otro si el contacto tiene un caso `landing` abierto de este flujo, o un toque de este flujo aceptado por Chatwoot (o de resultado desconocido, `delivery_unknown`) en las últimas 24 horas: `precheckout_contact_already_planned`. El ancla es por envío, así que este es el único freno de repetición.
- **La pérdida del consentimiento.** La reevaluación y el arranque vuelven a pedir la audiencia del scope; si ya no da `pilot_audience_allowed`, el motivo es `precheckout_authorization_lost`.

## Reevaluación y arranque del envío

El cliente de Supabase del bridge elige la RPC por el `anchor_type` de la acción. Para cualquier otra ancla llama a las de siempre, con el mismo cuerpo.

```text
public.reevaluate_portable_precheckout_action(...)       -- la firma de reevaluate_followup_action
public.mark_portable_precheckout_request_started(
  p_action_id uuid, p_attempt_id uuid, p_worker_id text,
  p_lease_generation bigint, p_now timestamptz
)                                                        -- la respuesta de mark_portable_payment_failure_request_started
```

- **Reevaluación.** Rechaza otra ancla con `55000 precheckout_intent_action_required`. Toma la intención (`for share`) **antes** que el contacto, el caso, la secuencia y la acción: es el orden de la admisión del formulario (intención, después contacto), así un reenvío del formulario de la misma persona y la reevaluación no se esperan en cruz. Ese lock además espera a una compra que se esté correlacionando con la intención. Con el caso vivo y la acción sin vencer, mira los frenos y la audiencia; si algo frena, cancela la acción, completa la secuencia y deja el caso `cancelled` con ese motivo, y registra `followup_action_reevaluated` con `decision = cancel`. Si nada frena, delega en `reevaluate_followup_action`, que queda intacta: una acción vencida o un caso que ya no está vivo los cierra ella, igual que el resto de sus chequeos. Las dos reevaluaciones del dispatcher (la del claim y la previa al envío) pasan por acá.
- **Arranque.** Exige el ancla `precheckout_intent`, autoriza con la frontera del piloto para `landing` / `PRECHECKOUT_FORM_SUBMITTED` y, si no es un replay, toma el lock de opt-out de las dos formas del teléfono (en orden) y vuelve a mirar los frenos. Un freno es `55000 pilot_request_start_rejected` con el motivo en `detail`. El rechazo deshace la autorización: no consume cupo. Después delega en `mark_followup_request_started`. Sin la frontera del piloto el cliente no la llama (`precheckout_intent_pilot_boundary_required`).
- **Plantilla.** El dispatcher manda la de `[plantillas.precheckout]` con sus `parametros`, en el modo directo ([approved-template-direct-dispatch-v1.md](approved-template-direct-dispatch-v1.md)). Sin esa plantilla configurada no manda nada: cierra el intento con `first_touch_template_not_configured`, sin reintento, antes del catálogo, del gate final y del arranque. El sender repite el corte antes de tocar Chatwoot. **Nunca usa la del carrito.**
- **Destinatario.** Se resuelve antes del gate final, con la regla de [Teléfonos](#teléfonos-las-dos-formas-del-mismo-móvil).

Las cuatro RPC nuevas (`admit_and_plan_portable_lead_precheckout`, `reevaluate_portable_precheckout_action`, `mark_portable_precheckout_request_started` y `get_portable_precheckout_pilot_runtime_status`) son ejecutables solo por `service_role`. Las funciones privadas (`_portable_precheckout_stop_reason`, `_find_portable_precheckout_contact`, `_ensure_portable_precheckout_contact` y `_plan_portable_precheckout_first_contact`) no las ejecuta ningún rol de la API.

## Readiness

Con el flag, `/ready` consulta `get_portable_precheckout_pilot_runtime_status` para el scope del flujo y suma una clave:

```text
portable_precheckout_first_contact: inactive | armed | paused | closed
```

| HTTP | `detail` | Cuándo |
|---|---|---|
| `503` | `portable_precheckout_readiness_unavailable` | La base no está configurada o la lectura falla |
| `503` | `portable_precheckout_pilot_runtime_config_invalid` | Falta algún parámetro del scope |
| `503` | `portable_precheckout_pilot_scope_config_mismatch` | El scope no está publicado, es de otro tenant o canal, no es de fuente `landing` / `PRECHECKOUT_FORM_SUBMITTED`, o es `manual_cohort` |
| `503` | `portable_precheckout_pilot_active_scope_mismatch` | El control está en otra versión |

Un scope bien configurado responde `200` aunque esté `inactive`: el proceso es desplegable sin que el flujo esté armado. Sin el flag la clave no aparece y `/ready` no cambia.

El healthcheck de `compose.yaml` usa `/ready`: prender el flag antes de publicar el scope deja el contenedor en `unhealthy`. El scope se publica primero.

## Logs

Una línea por envío admitido con el flag:

```text
portable_precheckout_first_contact admission=<outcome> plan=<plan_outcome|-> reason=<plan_reason|-> submission_id=<uuid>
```

Sale como warning solo si el plan es `plan_failed`; si no, como info (bajo uvicorn, sin configuración de logging, solo los warnings llegan a la salida del contenedor). Lleva ids y códigos: nunca el nombre, el email ni el teléfono. El motivo de cada envío queda además en el renglón del plan.

## Privacidad

- **El contacto se crea solo con el scope armado, y nunca en `consented_intent_in_cohort`.** Con el scope `inactive`, `paused` o `closed`, un formulario no deja filas nuevas en `contacts`, `contact_points` ni `channel_identities`: queda la intención, como sin el flujo, y el renglón del plan con el motivo.
- **El ancla y el renglón del plan llevan solo ids y códigos.** El `payload` del `webhook_events` del ancla no trae nombre, email ni teléfono, y el renglón no tiene columnas para esos datos.
- **Ningún log lleva el número.** Las líneas de este flujo y las de la equivalencia de teléfonos llevan ids de la base o de Chatwoot, códigos y, a lo sumo, la región (`MX`, `AR`, `other`).

## Invariantes

- **Un primer contacto por envío, frenado por la persona.** Cada envío nuevo tiene su ancla y puede planificar, pero no mientras la persona tenga un primer contacto vivo ni en las 24 horas siguientes a un toque.
- **Un formulario que llega con el scope desarmado no se planifica después.** Armar el scope no recorre los envíos anteriores: esos quedan `not_planned` con `pilot_runtime_not_armed`. Solo un envío nuevo, con el scope armado, planifica.
- **La demora corre desde el envío que dispara el plan.** Un reenvío del formulario sobre una intención de ayer sale hoy más la demora. El `submitted_at` es el del evento canónico: con el adaptador de GHL es el instante en que el bridge recibió el webhook, porque GHL no manda la hora del envío.
- **La compra cancela, no gana.** Una compra antes del toque cierra el caso `cancelled` con `intent_purchased` (o el freno que corresponda) y no consume cupo.
- **La identidad que compró queda frenada.** `purchase_by_identity` no tiene ventana: un email o un teléfono con una compra aprobada admitida del binding no vuelve a recibir este primer contacto en esa base. Vale también para un teléfono de prueba.

## Teléfonos: las dos formas del mismo móvil

El mismo móvil llega con dos formas. El formulario (el adaptador de GHL y `/webhooks/lead`) guarda `52` + 10 dígitos en México y `54` + 10 en Argentina; Hotmart y el `wa_id` de WhatsApp traen `521` + 10 y `549` + 10.

- **Se compara en forma canónica y no se reescribe nada al guardar.** `_whatsapp_phone_canonical` (SQL) y `canonical_whatsapp_phone` (`bridge/phones.py`) dejan solo los dígitos y reescriben únicamente `521` + 10 a `52` + 10 y `549` + 10 a `54` + 10. La regla va anclada por largo (13 dígitos). Brasil (el noveno dígito) queda afuera: no hay medición.
- **Con qué forma se manda y se crea el contacto de Chatwoot** lo decide una sola función, `whatsapp_delivery_phone`:

  | Teléfono canónico | Forma de entrega |
  |---|---|
  | México, `52` + 10 | `521` + 10 |
  | Argentina, `54` + 10 | `54` + 10 |
  | cualquier otro | igual a sí mismo |

  La regla sale de una medición del 2026-10-01 sobre el Chatwoot de producción (versión 4.13), solo con conteos:
  - **México.** Los 15 contactos mexicanos del inbox medido que escribieron tienen el identificador de WhatsApp con el `1`; ninguno sin él. Los 44 contactos `52` + 10 los creó el bridge al mandar una plantilla y ninguno tiene un mensaje entrante. Hay 14 pares de la misma persona con un contacto `52…` (donde salió la plantilla) y otro `521…` (donde cayó su respuesta, en otra conversación). Chatwoot 4.13 no tiene normalizador para México.
  - **Argentina.** Chatwoot sí normaliza: a un entrante `549…` le busca primero un contacto `54…`. Una plantilla mandada a `54` + 10 se entregó y la respuesta cayó en la misma conversación.

  La medición es de otro inbox. **No prueba el envío en ATT1:** ahí la regla se confirma en el E2E, antes de prender un flujo.
- **Antes de crear un contacto en Chatwoot se buscan las dos formas.** Si ya existe uno, se usa ese con su `source_id`. Si existen los dos, el de la forma de entrega (`whatsapp_delivery_phone`: `521…` en México, `54…` en Argentina), que es donde Chatwoot resuelve la respuesta, con un aviso que lleva el inbox, la región y los ids de Chatwoot, nunca el número.
- **El gate final recibe el `wa_id` que se va a usar.** El dispatcher resuelve el destinatario antes de la reevaluación final, del gate y del arranque. Si ese `wa_id` no es canónicamente el teléfono consentido, cierra el intento con `chatwoot_recipient_phone_mismatch`, sin reintento. Si Chatwoot no responde a la búsqueda, `pre_request_failed`.
- **El teléfono del contacto tiene que ser el consentido.** El envío sale a `contacts.phone`; si no es canónicamente el de la intención, la audiencia rechaza con `consented_intent_contact_phone_mismatch`, al planificar y al arrancar.
- **El opt-out se mira en las dos formas.** Un opt-out guardado bajo `521…` frena a una intención en `52…`, al planificar (`precheckout_prior_opt_out`, y `consented_intent_prior_opt_out` en carrito y pago fallido) y al arrancar.

El detalle de la migración y de lo que cambia para carrito y pago fallido está en [commercial-ally-runtime-v1.md](commercial-ally-runtime-v1.md#equivalencia-de-teléfonos-de-whatsapp-2026-10-01-migración-20261001000100).

## Qué no hace

- No recorre los envíos anteriores al armado del scope ni reintenta un envío `not_planned` o `plan_failed`.
- No verifica el envío del formulario contra GHL: con el adaptador, la barrera es el token y la aceptación escrita del riesgo.
- No limita el total de mensajes proactivos por persona entre flujos. Un carrito o un pago fallido abiertos frenan este primer contacto, pero quien recibió este toque puede recibir después el del carrito. El tope entre flujos es una decisión abierta, anterior a abrir carrito y primer contacto juntos en `consented_intent`.
- No resuelve a quien escribió primero por WhatsApp y después abandona el carrito o falla el pago: ese evento de Hotmart no encuentra el contacto del entrante y falla cerrado (se pierde la recuperación, no se manda nada indebido).
- **No conversa con quien responde a la plantilla.** Es un límite heredado del runtime portable y vale igual para carrito y pago fallido: la aceptación del envío deja la conversación del caso con `automation_status = 'enabled'`, y la admisión entrante (`admit_inbound_commercial_case_v2`) solo toma una conversación `draft_only`, así que rechaza cada respuesta a la plantilla con `22000 inbound_canonical_conversation_conflict`. No hay respuesta del agente, ni enlace, ni derivación. Lo fija `validate_att1_portable_chain.mjs` (caso 9.4b). Que el agente entrante adopte la conversación que abrió una plantilla es una decisión de diseño pendiente (la planificación de descuento posterior a la respuesta lee esa misma conversación en `enabled`), y los botones `QUICK_REPLY` de las plantillas todavía no tienen un payload capturado. **Ningún flujo de salida se abre a personas reales antes de resolverlo.**
  - **Lo que sí funciona sobre esa conversación es la baja.** Con manifiesto, si la admisión falla el bridge mira igual el historial: un «No más mensajes» (o cualquier frase de baja) se registra con el opt-out durable, y a quien ya se había dado de baja se le reconcilia el stop. En los dos casos el trabajo termina.
  - **El resto no se reintenta sin límite.** Un rechazo determinista de la admisión deja `chatwoot_cut_b_admission_rejected conversation=<id> reason=<código>` en el log y el mensaje termina como `failed` tras los intentos acotados del worker. Una falla transitoria (la base caída) se sigue reintentando.

## Lo que no está medido

- **El envío en ATT1.** Ningún mensaje de este flujo salió por el inbox de ATT1. La forma de entrega, el `wa_id` con que responde Meta y la conversación en que cae la respuesta se confirman en el E2E.
- **Argentina con `549`.** No hay ningún contacto `549` medido en el inbox del 2026-10-01.
- **PostgREST real.** La forma de las filas que devuelven las cuatro RPC por PostgREST no se probó contra un PostgREST real: los tests usan la del `returns table`.
- **Los locks de `_ensure_portable_precheckout_contact`.** Dos formularios de la misma persona a la vez dejan un contacto y un caso, pero lo que los serializa es la admisión del formulario, que bloquea el binding de la instancia hasta el fin de la transacción. Los locks por teléfono y por email del contacto son un segundo cinturón que ninguna prueba ejercita por separado.
- **El check de `recovery_cases.source` en la base de Johanna.** Nació sin nombre (inline en la tabla), así que la migración lo busca por definición: el único check de la tabla sobre `source` que acepta exactamente `hotmart` y `simulator`. Si en una base hay cero o más de uno, la migración aborta entera con `55000 recovery_cases_source_check_not_found` y el `detail` dice cuántos encontró. No se leyó la base de Johanna para confirmarlo.

## Riesgos conocidos

- **La compra que entra por el camino compartido.** En una instancia sin `PORTABLE_HOTMART_PURCHASE_STOP_ENABLED` la compra la cierra el trigger viejo y el caso queda `won`, no `cancelled`. El flag del flujo exige ese stop, así que con el flujo prendido no aplica.
- **Dos identidades del mismo móvil.** El plan del primer contacto crea la identidad con el teléfono del formulario y el evento de Hotmart podía crear otra con el suyo. Desde esta versión el worker de resolución reutiliza la que el contacto ya tiene ([commercial-ally-runtime-v1.md](commercial-ally-runtime-v1.md#equivalencia-de-teléfonos-de-whatsapp-2026-10-01-migración-20261001000100)). Un contacto que ya quedó con las dos (creadas antes, o por una lectura que falló) sigue resolviendo el entrante con el `wa_id` textual y un aviso `chatwoot_inbound_identity_duplicated`: elegir entre dos identidades por la conversación que ancla cada una queda pendiente.
- **Carrera con el worker de resolución.** `resolve_event` crea contactos sin los locks del formulario. Si se cruzan pueden quedar dos contactos de la misma persona, y el formulario siguiente da `precheckout_contact_ambiguous`. Falla cerrado.
- **El costo del freno por compra.** Recorre las compras admitidas del binding sin un índice por email ni teléfono. A la escala de una instancia no pesa; con muchas compras habría que indexar.
- **Un rechazo del arranque** (`pilot_request_start_rejected`: un tope del scope, el piloto desarmado, o un freno que entró después de la reevaluación final) no corta el lote: el dispatcher deja el motivo en el log (`durable_request_start_rejected action_id=… attempt_id=… anchor=… reason=…`) y sigue con las demás acciones. El intento **queda reservado** a propósito: cerrarlo sin reintento dejaría la acción `permanent_failed` y el caso abierto para siempre. Lo resuelve el lease siguiente: con un freno, la reevaluación cancela el caso con ese motivo; con un tope o el piloto desarmado, la acción se vuelve a intentar en cada vencimiento del lease (5 minutos) hasta que el arranque pasa o la acción vence con su caso. Cada intento reserva un renglón nuevo en `followup_delivery_attempts`.

## Pruebas

- `tests/sql/followup_engine/validate_portable_precheckout_first_contact.mjs` (PGlite, dentro de `npm test`): la cadena con la reevaluación y el arranque reales, con los goldens del traductor de GHL (`tests/fixtures/ghl/expected/`) y la política y el scope del fixture de la instancia. Cubre la demora, cada freno (la compra por identidad también por teléfono y por punto de contacto; la derivación al planificar, al reevaluar y al arrancar), la ventana de 24 horas en sus dos bordes, las dos entregas con un solo caso, el scope desarmado sin filas nuevas, el contacto del entrante completado, el check de `source` quitado por definición y el ACL.
- `tests/sql/followup_engine/validate_att1_portable_chain.mjs`: el tramo del primer contacto en el orden del E2E de la instancia, y la compra en `521` que lo cancela.
- `tests/test_portable_precheckout_first_contact_migration.py`: la migración contra las definiciones vigentes que copia.
- `tests/sql/followup_engine/real_postgres_portable_precheckout_first_contact.py`: lo que depende del servidor, sobre un Postgres 17 real con los privilegios por defecto de Supabase y el inventario de ACL completo. El bloque con los triggers diferidos, los entrypoints como `service_role`, y lo que solo se ve con dos sesiones: dos formularios a la vez, la reevaluación contra un reenvío del formulario (sin `40P01`), la reevaluación que espera a una compra en vuelo, el arranque que espera a un opt-out en vuelo desde la otra forma del teléfono, y el `lock timeout` adentro del plan que se relanza sin admitir el formulario. Corre en el CI (paso *Verify portable first contact on PostgreSQL*).
- `tests/test_instance_wiring.py`, `tests/test_lead_precheckout_http.py`, `tests/test_ghl_precheckout_adapter_http.py`, `tests/test_supabase.py`, `tests/test_worker.py`, `tests/test_messaging.py`, `tests/test_durable_dispatcher_approved_template.py` y `tests/test_att1_production_settings.py`: el arranque, los dos caminos del formulario, `/ready`, las RPC por ancla y la plantilla.
- `tests/e2e/test_att1_production_like_final_gate.py`: el envío capturado de GHL entra por el adaptador y el dispatcher frena en el gate final cerrado, con el hash del `wa_id` resuelto.

**Deuda de fixtures.** No hay un `lead.precheckout` capturado de ATT1, ni `PURCHASE_APPROVED` o `PURCHASE_CANCELED` capturados, ni las respuestas de Chatwoot a `/contacts/search` y al envío: los tests usan los precedentes inline del repo.
