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
paises = ["MX", "US"]                    # el selector; default una lista de 21 países
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

## Riesgos y límites

- La ruta es pública: la barrera es el origen, el campo trampa y el límite por IP y por teléfono. Un script que falsifique el `Origin` puede registrar una intención con consentimiento para un teléfono ajeno (el mismo riesgo del token del adaptador de GHL). Turnstile queda para una versión siguiente.
- El límite es en memoria: se reinicia con el bridge.
- La página propia no dispara pixel; el embebible tampoco (lo dispara la página que lo aloja).
