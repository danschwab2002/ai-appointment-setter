# Contrato: formularios propios (own-forms-v1)

Estado: propuesto el 2026-11-26. Reemplaza al formulario de GHL en las landings de Lancemos (hito «Maribel y ATT1 sin GHL»). La evidencia de funcionamiento va a `docs/operations/` después del primer envío medido.

## Qué es

Un formulario que dibuja el bridge de cada instancia y que, al enviarse, entra directo al recuperador por la misma admisión que hoy usa el adaptador de GHL (`ghl-precheckout-adapter-v1`). Sale de dos formas:

- **Página propia:** `GET /f/<clave>`, el formulario solo en el dominio del bridge.
- **Embebible:** `GET /f/<clave>/embed.js`, que dibuja el formulario en cualquier página adentro de `<div data-setter-form="<clave>"></div>` (o justo después del `<script>` si no hay contenedor).

## La definición (manifiesto)

```toml
[[formularios_propios]]
clave = "att1-org-b"                     # a-z, 0-9, guiones; 3 a 40
landing_id = "alimenta-tu-tiroides"      # la landing de una oferta del manifiesto
# opcionales
aclaracion = "Al enviar, acepto ..."     # default: la frase de los formularios de GHL de hoy
links = [{ texto = "Política de Privacidad", url = "https://..." }]
titulo = ""                              # arriba del formulario
titulo_pagina = "Dra. Nina Garza"        # <title> de la página propia; default [instancia].marca
boton = "Enviar"
origenes = ["https://site.metodoraizana.com.mx"]   # desde dónde se puede enviar el embebible
pais = "MX"                              # país preseleccionado
paises = ["MX", "US"]                    # el selector; ausente o vacío, una lista de 21 países con México primero
gracias_url = "https://..."              # si está, va ahí en vez del checkout
webhooks = ["https://..."]               # copia del envío, como la acción Webhook de GHL
usar_core = true                         # en una página del core, el core arma el checkout
```

Declarar un formulario equivale a listarlo en `[adaptadores.ghl].formularios`: sus envíos entran con `consent.copy_version` de `[consentimiento]` y `whatsapp_contact = true`. Exige el evento `intencion`.

## El envío

`POST /f/<clave>/enviar`, `Content-Type: application/json`, cuerpo de hasta 8 KB:

```json
{"nombre": "...", "email": "...", "telefono": "+52...", "pagina": "<location.href>",
 "referrer": "<document.referrer>", "website": "", "fbc": "<cookie _fbc>", "fbp": "<cookie _fbp>"}
```

El bridge arma el mismo `lead.precheckout` 1.1.0 que el adaptador de GHL (mismas reglas de teléfono, incluido el `+521` de México, del `sck` del core y de los caracteres que la base no guarda), con la landing y la oferta del formulario y la atribución de la URL de `pagina` (`fbclid` de la URL o, si no, de `_fbc`). Lo valida con `parse_lead_precheckout` y lo admite por `_admit_portable_lead`, el mismo camino del adaptador: el envío queda en `precheckout_submissions` con el evento como `raw_payload`, y según los flags de la instancia planifica el primer contacto o entra al motor nativo.

Respuestas:

| Código | Cuerpo | Cuándo |
|---|---|---|
| 200 | `{"status": "received" \| "duplicate" \| "conflict", "checkout_url", "gracias_url"}` | admitido. `checkout_url` es el link de Hotmart como lo arma el core (`off`, `checkoutMode=10`, `name`, `email`, `phone`, `phoneac`, `phonenumber`, UTM, `sck`, `fbclid`) |
| 200 | `{"status": "received", "checkout_url": null, ...}` | el campo trampa vino lleno: no se guarda nada |
| 400 | `{"detail": {"codigo": "cuerpo_invalido"}}` | no es JSON |
| 403 | `{"detail": {"codigo": "origen"}}` | el `Origin` no está en `origenes` ni es el del bridge |
| 413 / 415 | `largo` / `tipo` | cuerpo grande / no es JSON |
| 422 | `{"detail": {"codigo": "nombre" \| "email" \| "telefono", "mensaje"}}` | dato inválido; `mensaje` se le muestra a la persona |
| 429 | `limite` | más de 10 envíos por IP en 10 minutos o 3 por teléfono en una hora (en memoria, por proceso) |
| 503 | `no_disponible` | la base no responde |

Después de responder, si el formulario declara `webhooks`, el bridge les manda en segundo plano, un intento cada uno, `{"event": "own_form.submitted", "version": "1", "form", "created_at", "contact", "landing", "offer", "attribution", "consent"}`.

## En el navegador

`embed.js` valida, manda el envío y con un 200 dispara `setter-form:enviado` en el contenedor. Si la página es del core de Lancemos (`window.superpowers.formSubmitted`) y `usar_core` no es `false`, le entrega el contacto al core, que arma el checkout con todo su tracking (incluidos `src` de VTurb y `_gl` de GA4), igual que hoy con el formulario de GHL. Si no, va a `gracias_url` o a `checkout_url`. En el core, la landing se configura con `form: 'propio'`.

## Activación

`OWN_FORMS_ENABLED=true` por instancia. Apagado, las tres rutas dan 404 y `/ready` no cambia; prendido, `/ready` dice `own_forms: enabled:<n>`. Con el flag prendido el bridge no arranca sin el evento `intencion`, sin `[[formularios_propios]]` o sin el origen público (`OWN_FORMS_PUBLIC_BASE_URL`, o en su defecto `FLOW_APPROVAL_BASE_URL`).

## Registro a un evento (4.3, propuesto el 2026-11-26)

Propuesto, sin desplegar ni activar. Un formulario propio puede ser la inscripción a un evento (una clase gratuita, un vivo) en vez del formulario de la VSL antes del checkout:

```toml
[[formularios_propios]]
clave = "att1-clase-28-11"
landing_id = "alimenta-tu-tiroides"
tipo = "registro"                  # default "precheckout": el de arriba
evento = "clase-28-11"             # a-z, 0-9, guiones; 3 a 40. Solo con tipo = "registro"
gracias_url = "https://..."        # obligatoria con tipo = "registro" (D-43.5)
usar_core = false                  # obligatorio con tipo = "registro" (D-43.5)
```

- **D-43.5.** Un formulario de registro exige `gracias_url` y `usar_core = false`: la persona que se anotó a una clase nunca termina en el checkout de Hotmart.
- **D-43.3 · la admisión.** Con `OWN_FORMS_REGISTRATION_FLOW_ENABLED`, `OWN_FORMS_ENABLED`, `FLOW_ENGINE_ENABLED` y `FLOW_ENGINE_NATIVE_INTENT_ENABLED` prendidos, el envío de un formulario de registro se admite con `admit_portable_observed_lead_precheckout` (el `lead.precheckout` 1.1.0 de siempre, guardado en `precheckout_submissions`) y **no se planifica nada**: ni el primer contacto comercial del legado ni un flujo nativo de `intencion`. La respuesta HTTP es la misma. Log: `own_form_registration_admitted form=<clave> evento=<clave> product=<producto> admission=<...> flow=sin_ruteo_de_la_base submission_id=<id>` (solo ids y códigos).
- **D-43.4 · los flags.** Con cualquiera de los cuatro apagado, un formulario de registro va exactamente como hoy (como uno de `precheckout`). El bridge arranca igual; `/ready` dice `own_forms_registration: inerte:<flags que faltan>` con el flag del registro prendido, o `admite_sin_flujo:<formularios de registro>` con los cuatro. Sin el flag, `/ready` no cambia.
- **La confirmación** la manda un flujo con `dispara: {evento: intencion, registro: <evento>}` (flow-definition-v1, D-43.2), desde la segunda parte (abajo). Sin ella, la regla 14 no lo deja publicar y **no sale ningún mensaje**.
- **La pieza de la base (segunda parte, propuesta el 2026-11-27, migración `20261127500000`, sin desplegar ni activar).** Con los cuatro flags, el bridge manda el envío a `admit_and_route_registration_lead_precheckout_v1` con la clave del evento del formulario (`p_registration_event`: el envío crudo no la trae, medido en el envío de prueba de ATT1 del 2026-11-26) y el freno por salud (`p_health_brake`). La base admite el envío como siempre y lo prueba solo en los flujos nativos del producto cuya versión activa tiene `dispara: {evento: intencion, registro: <esa clave>}`; nunca el legado. Sin un flujo así: `ruta = sin_flujo` y solo la admisión. Una base sin la entrada (404): lo de la primera parte, con `own_form_registration_rpc_missing` en el log.
  - **D-43.6 · el disparador.** `_flow_engine_native_trigger` reconoce `{evento: intencion, registro: <clave>}` (y nada más, con la clave del esquema) como `intencion`: el caso, el permiso (el envío consentido que lo disparó), la reevaluación con los frenos del legado, el arranque con los locks `chatwoot-opt-out-user`, el tope por persona (§35) y la forma de los pasos (`nativo:intencion`) son los del primer contacto nativo. El caso lleva `context.registration_event`.
  - **D-43.7 · una sola confirmación.** La capacidad `intencion:registro` del motor nativo (`ENGINE_CAPABILITIES_NATIVE_REGISTRATION`) es la del primer contacto de un toque: una plantilla con nombre y producto, sin botón declarado ni espera después. La base dice lo mismo (`motor_nativo:forma` con dos plantillas). Un recordatorio antes del evento queda para otra pieza.
  - **D-43.8 · la separación.** El formulario comercial no toma un flujo de registro (la selección excluye `dispara.registro`) y su toque nativo de 24 h no cuenta una confirmación. La repetición de D21 cuenta los recorridos comerciales entre sí y los de registro solo con el mismo evento: una confirmación enviada no le quita el primer contacto comercial a nadie, ni al revés, y un segundo registro al mismo evento es `ya_disparado` (con `otra_despues_de: nunca`). El doble caso con el primer contacto del legado no frena una confirmación. Límite aceptado: mientras el caso de la confirmación está abierto (sale sin espera), el legado de ATT1 ve un caso del formulario de la persona y no planifica su primer contacto comercial de ese momento (`precheckout_contact_already_planned`), como con un primer contacto nativo.
  - **D-43.9 · el freno por salud de Meta.** El bridge lee la salud con la misma lectura y el mismo veredicto que el disparo por audiencia (`FLOW_AUDIENCE_META_HEALTH_PATH`, `FLOW_AUDIENCE_META_HEALTH_KEY`, `health_verdict`: rojo o amarillo, `BLOCKED`, límite de 2.000 o menos, lectura de más de 26 h o ilegible). Con un freno, la base no crea contacto, ancla ni caso: cada flujo deja `motor_no_arranca` con `meta_salud:<qué>` en `flow_engine_native_intent_admissions` y en `flow_engine_start_rejections` (MOT-002 lo avisa). Sin el archivo, frena (`meta_salud:sin_lectura`). Como en la audiencia, el freno se mira al admitir y no otra vez al mandar: la confirmación sale sin espera.
  - **D-43.10 · la regla 14.** Con los cuatro flags el bridge publica `native_registration_enabled: true` en el runtime de `/ready` y la regla 14 deja publicar el flujo; apagado, el runtime es el de hoy (la clave no aparece) y el flujo sigue «todavía no».
  - **D-43.11 · `/ready`.** Con los cuatro flags, `own_forms_registration: confirma:<formularios de registro>`, y `:sin_salud` al final si falta el archivo de la salud.

## Riesgos y límites

- La ruta es pública: la barrera es el origen, el campo trampa y el límite por IP y por teléfono. Un script que falsifique el `Origin` puede registrar una intención con consentimiento para un teléfono ajeno (el mismo riesgo del token del adaptador de GHL). Turnstile queda para una versión siguiente.
- El límite es en memoria: se reinicia con el bridge.
- **La simulación de un flujo comercial de `intencion` todavía cuenta los registros.** La simulación de un flujo de registro (`dispara: {evento: intencion, registro: <evento>}`) no cuenta nada y no comparte el evento con los comerciales (deuda de #532, `flow-change-cycle-v1`). Al revés no: la de un `nuevo`, `prender` o `version` de un flujo comercial de `intencion` cuenta con `read_flow_engine_native_trigger_counts_v1` todos los formularios consentidos del producto, también los de registro, porque la base no sabe qué envío es de un registro (la clave del evento la manda el bridge, D-43.6). Con registros en el mismo producto (el mismo funnel), el número es un máximo. Separarlos pide que la base guarde la clave del evento con el envío.
- La página propia no dispara pixel; el embebible tampoco (lo dispara la página que lo aloja).
