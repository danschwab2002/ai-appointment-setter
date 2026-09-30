# Contrato: adaptador del formulario de GHL a `lead.precheckout` (v1)

- **Estado:** contrato v1, escrito antes de la implementación (2026-09-30) e implementado en el bridge 1.1.0 (sin publicar), apagado por defecto. No describe nada desplegado.
- **Endpoint:** `POST /webhooks/adapters/ghl/lead-precheckout`
- **Emisor:** la acción *Webhook* de un workflow de GHL con disparador *Form submitted*. Es la forma medida el 2026-09-29 por el receptor temporal `ghl-capture-att1` (workflow `b3304158-ec6b-4491-8057-92f695da3db1`).
- **Salida:** `lead.precheckout` `1.1.0` ([lead-precheckout-v1.md](lead-precheckout-v1.md)), validado por `parse_lead_precheckout` y admitido por `admit_portable_observed_lead_precheckout`, el mismo camino que un formulario de landing en un runtime con manifiesto.
- **Origen:** [ADR-0021](../decisions/0021-setter-producto-instalable.md), punto 8 (el centro solo entiende eventos canónicos y cada fuente entra por un adaptador), y la decisión del 2026-09-29 de que el formulario de ATT1 entra por un adaptador del webhook de GHL. Consentimiento: decisión `2026-09-29-att1-consentimiento-por-aclaracion-al-enviar-sin-casilla`.

## Propósito y límite

Traduce el POST que GHL manda cuando alguien envía un formulario de la landing al evento canónico, y lo admite por el camino de siempre. No escribe nada propio, no guarda el cuerpo de GHL (en `precheckout_submissions` queda solo el evento traducido) y no decide nada comercial.

Listar un formulario en `[adaptadores.ghl].formularios` afirma dos cosas que el adaptador no puede verificar:

1. que el formulario muestra la aclaración de `consentimiento.copy_version`, porque cada envío sale con `whatsapp_contact = true`;
2. que el envío es el paso previo al checkout de la oferta de esa landing, el mismo hecho que `lead.precheckout` registra.

Un formulario se suma a la lista solo después de verificar las dos.

## Transporte

```text
POST /webhooks/adapters/ghl/lead-precheckout
Content-Type: application/json
X-Setter-Adapter-Token: <token>        (opcional si el token va en el cuerpo)
```

- **Autenticación.** GHL no firma. La autenticación es `GHL_PRECHECKOUT_ADAPTER_TOKEN` (32 caracteres o más), en el header `X-Setter-Adapter-Token` o en `customData.setter_token`. La acción *Webhook* estándar (la medida) manda `customData` en el cuerpo; que acepte headers no está medido. Si los acepta, se prefiere el header. Se compara en tiempo constante.
  - Un header presente y distinto da `401` sin leer el cuerpo.
  - Header y cuerpo presentes y distintos: `401`.
  - El token nunca va en la ruta ni en la query: el access log de uvicorn las registra.
- **El token es la única barrera.** El id del formulario y las URL de las landings son públicos (el id está en la URL del widget; las URL, en los anuncios). Quien lea el token en la configuración del workflow de GHL, es decir cualquier usuario de la subcuenta, puede admitir intenciones con cualquier teléfono y `whatsapp_contact = true`. La lista de formularios y la resolución de la landing filtran errores de configuración, no a quien tiene el token. Ver [Riesgos](#riesgos).
- **Content-Type.** El tipo de medio tiene que ser `application/json`; el charset es opcional. El `User-Agent` no se valida (`axios/0.21.4` es un detalle interno de GHL).
- **Tamaño.** Cuerpo de 64 KiB como máximo, igual que `/webhooks/lead`. Las capturas miden 2,7 KB y 4,6 KB.
- **Claves repetidas.** GHL pone los campos personalizados como claves de primer nivel con su nombre visible, en el mismo espacio que `email`, `phone` y `contact_id`. Un objeto JSON con una clave repetida, en cualquier nivel, da `400 ghl_invalid_payload`: el adaptador nunca elige en silencio cuál de las dos vale.
- **Frescura.** No hay ventana: GHL no manda la hora del envío (`date_created` es el alta del contacto). La defensa contra un replay es el token.

## Configuración

- `GHL_PRECHECKOUT_ADAPTER_ENABLED`, `false` por defecto. Con `false`, la ruta responde `503`.
- Prendido, el bridge no arranca si falta alguna de estas condiciones:
  - manifiesto v2 (`INSTANCE_MANIFEST_PATH`);
  - `"intencion"` en `eventos`;
  - `[adaptadores.ghl]` con al menos un formulario;
  - un token de 32 caracteres o más, distinto de todo otro secreto y valor de texto de la configuración del bridge (`LEAD_PRECHECKOUT_SECRET`, `CHATWOOT_WEBHOOK_SECRET`, el token del primer contacto, los de Hotmart, Slack, Supabase, Hermes, OpenRouter, etc.): lo lee cualquier usuario de la subcuenta de GHL, y repetido le daría esa otra autoridad;
  - `[flujos].precheckout` en `false` (ver [Riesgos](#riesgos)).
- Apagado, ninguna de esas condiciones se evalúa: un runtime sin token ni manifiesto arranca igual que hoy.
- No depende de `LEAD_PRECHECKOUT_ENABLED` ni de su secreto.
- `/ready` agrega `ghl_precheckout_adapter: enabled:<n>-forms` solo con el flag prendido, y nunca responde `503` por el adaptador.

```toml
eventos = ["carrito", "pago_fallido", "compra", "entrante", "intencion"]

[adaptadores.ghl]
formularios = ["EgDqRl2xWc59YjVW1q8W"]   # attributionSource.mediumId; 20 alfanuméricos
```

## Forma de entrada (medida)

Hay dos capturas del 2026-09-29, anonimizadas, en `tests/fixtures/ghl/`. Cada una es un sobre con `_capture` (origen, fecha, qué es, qué se anonimizó y qué relaciones entre los objetos de atribución se midieron en el original), `headers` (los que mandó GHL) y `payload` (el cuerpo tal cual). El adaptador solo ve `payload`.

- **El cuerpo.** Es un objeto plano con los campos estándar del contacto (`contact_id`, `full_name`, `email`, `phone`, `date_created`, `tags`, `country`, `timezone`, `location`), más `workflow`, `triggerData`, `customData` y tres objetos de atribución:
  - `attributionSource`, de primer nivel: el envío que disparó el workflow. En el envío real de `ads-a` viene con `url`, `medium = "form"`, `mediumId` y un `fbEventId` propio del envío (distinto del del primer toque);
  - `contact.attributionSource`: el primer toque del contacto;
  - `contact.lastAttributionSource`: el último toque.
- **La prueba del editor de GHL.** Llega con `attributionSource = {}`. No es un envío.
- **Campos personalizados.** Cuando el workflow agrega campos personalizados, van como claves de primer nivel con el nombre visible del campo.
- **Datos que no sirven.**
  - `country` dice `US` para teléfonos de México, así que no sirve para el país.
  - Los campos estructurados de UTM pierden datos: GHL guarda `campaign` en minúsculas en el primer toque, y `utm_id` solo existe en la URL.

## Mapeo

| `lead.precheckout` | De dónde sale | Regla |
|---|---|---|
| `id` | aleatorio | ULID nuevo en cada traducción (`generate_issuance_ulid`). No sale de ningún campo de GHL: ver [Reintentos](#reintentos) |
| `event`, `version` | fijos | `lead.precheckout`, `1.1.0` |
| `created_at` | reloj del bridge | Momento de la traducción, en UTC con `Z` |
| `source.system` | fijo | `landing` (lo exigen el parser y la RPC; la procedencia GHL queda solo en el log) |
| `source.site`, `source.landing_id` | `attributionSource.url` | Host en minúsculas y ruta, sin query ni fragmento, comparados exactos contra `[[hotmart.ofertas]]` con una barra final tolerada de los dos lados (en la URL del envío y en la `url` de la oferta, que el manifiesto acepta con barra). Sin coincidencia: `422 ghl_landing_unknown`; más de una: `422 ghl_landing_ambiguous` |
| `source.page_url` | la oferta resuelta | `https://<host><ruta>` de la oferta del manifiesto: la query del envío (`?test=yes`, UTM) no pasa |
| `source.aliado` | manifiesto | `instancia.marca` |
| `data.buyer.name` | `full_name`, si falta `first_name` + `last_name` | Sin lo que la base no guarda (abajo) y recortado; vacío: `400` |
| `data.buyer.email` | `email` | Recorte y minúsculas; fuera de forma, o con algo que la base no guarda: `400` (nunca se reescribe a otra dirección) |
| `data.buyer.phone`, `phone_country_code`, `phone_national` | `phone` | `phonenumbers` sin región por defecto, después de normalizar el móvil mexicano con el `1` heredado (abajo). `phone` es el E.164 de `phonenumbers`; `phone_country_code`, el código de país; `phone_national`, los dígitos de `phone` que siguen al código. Un número que no se puede parsear, inválido, o de menos de 8 o más de 15 dígitos en total (`phonenumbers` da válidos números de 7, como los de Niue, y la RPC exige `^[1-9][0-9]{7,14}$`): `422 ghl_phone_unusable`, porque la admisión portable 1.1.0 los rechaza en cada entrega. Fuera de eso, el número se manda como lo guardó GHL: no se inserta el `9` de Argentina |
| `data.checkout_country` | región del teléfono | `{iso, source: "phone_country_code"}` |
| `data.product` | `[hotmart]` | `hotlink`, `product_name`, `precio`, `moneda`; `id = null` |
| `data.offer.code`, `data.checkout_url` | la oferta resuelta | `https://pay.hotmart.com/<hotlink>?off=<oferta>` |
| `data.attribution.utm_*` | query de `attributionSource.url` | Decodificada; `""` si falta. Nunca de los campos estructurados de GHL |
| `data.attribution.sck` | query de `attributionSource.url` | Ver abajo |
| `data.attribution.fbclid` | query de `attributionSource.url`; si no está, `attributionSource.fbclid` | Gana el de la URL, que es el que ve la landing. `""` si no hay ninguno |
| `data.attribution.referrer` | `attributionSource.referrer` | `""` si es `null` |
| `data.consent` | manifiesto | `marketing_optin = true`, `whatsapp_contact = true`, `copy_version = [consentimiento].copy_version` |
| `dedupe_key` | derivado | `<site>:<oferta>:<email normalizado>` |

### Lo que la base no guarda

`U+0000` no entra en un texto de `jsonb` (la RPC falla con `22P05`) y un surrogate suelto no se codifica en UTF-8. El parser deja pasar los dos, así que un evento con uno fallaría en cada entrega: `503`, y GHL reintentaría sin fin. Cualquiera los pone en la URL de la landing (`?utm_campaign=%00`). El adaptador los quita del nombre y de toda la atribución (`utm_*`, `sck`, `fbclid`, `referrer`) y conserva el lead; un email con uno de ellos da `400`. Los demás caracteres de control pasan como vienen, igual que por `/webhooks/lead`: la base los guarda.

### Móviles de México con `+521`

México dejó de marcar el `1` de los móviles en 2019, y `phonenumbers` 9.0.37 da inválido un `+521` seguido de 10 dígitos (y válido el mismo número sin el `1`). Antes de validar, `+521` seguido de exactamente 10 dígitos pasa a `+52` seguido de esos 10. Es traducción de formato, no una decisión comercial: sin ella, un lead mexicano guardado así por GHL se perdería entero con `422`. Ningún otro prefijo se reescribe. Las otras fuentes del mismo teléfono no normalizan: ver [Riesgos](#riesgos).

### El `sck`

Es el port literal del compositor del core de Lancemos (`readSck` y `sanearCampoSck`, estándares E01, E02, E10 y E13), así el link del agente conserva la misma etiqueta que habría compuesto la landing:

- **Con alguna UTM**, `utm_id` incluido: `utm_source~utm_term~utm_content~utm_medium~utm_campaign`, y `~utm_id` al final si vino. Las posiciones vacías se mantienen. Saneo de cada campo:
  - NFD sin diacríticos y sin caracteres de control;
  - `~` pasa a `-` y los espacios a `-`;
  - se elimina todo lo que quede fuera de `[A-Za-z0-9._~-]`;
  - se colapsan los guiones repetidos y se recortan en los bordes.
- **Sin UTM**: el `sck` de la URL, con controles quitados, espacios colapsados y recortado.
- **Sin ninguna de las dos**: `""`. No se inventa atribución.

El adaptador no valida el alfabeto ni el largo. La emisión del link descarta un `sck` fuera de `[A-Za-z0-9._|~-]{1,255}` y lo registra, igual que para una landing. La prueba son los vectores capturados del core (`tests/fixtures/lancemos_core_sck_*.json`).

## Qué no hace

- No lee `contact.attributionSource` (primer toque), ni `contact.lastAttributionSource`, ni los campos estructurados de UTM.
- No usa `fbEventId` para nada.
- No lee los tags (en la subcuenta de ATT1 los pone la automatización de recuperación de GHL), ni los campos personalizados, ni `country`, `timezone`, `contact_source` o `customData` fuera de `setter_token`.
- No acepta la prueba del editor de GHL ni cualquier POST sin `attributionSource` de primer nivel: `422 ghl_not_a_form_submission`.
- No trata distinto los envíos con `?test=yes`: son intenciones reales.
- No reintenta: si la admisión responde `503`, reintenta GHL.
- No verifica el envío contra GHL (no lee el contacto por la API de GHL).

## Respuestas

| HTTP | Cuándo |
|---|---|
| `200` | `received`, `duplicate` o `conflict`, con el cuerpo de `/webhooks/lead`: `status`, `delivery_id`, `purchase_intent_id`, `activation_authorized = false`, `contact_authorized = false` |
| `400` | JSON inválido (`ghl_invalid_json`); una clave repetida, un cuerpo que no es un objeto, o faltan `contact_id`, `email`, `phone` o el nombre (`ghl_invalid_payload`); `Content-Type` distinto (`invalid_ghl_transport`) |
| `401` | Token ausente o distinto (`invalid_adapter_token`) |
| `413` | Cuerpo mayor a 64 KiB (`ghl_adapter_body_too_large`) |
| `422` | `ghl_not_a_form_submission`, `ghl_form_not_allowed`, `ghl_landing_unknown`, `ghl_landing_ambiguous`, `ghl_phone_unusable` o `ghl_translation_rejected`. Ninguno toca la base |
| `503` | Adaptador apagado (`ghl_precheckout_adapter_not_enabled`), base no configurada (`supabase_not_configured`) o admisión no disponible (`ghl_precheckout_persist_unavailable`) |

## Reintentos

Cada entrega, reintento de GHL incluido, es una submission nueva con su propio `id` aleatorio y su propio `created_at`. La admisión la enlaza a la misma intención viva (misma oferta, mismo email y mismo teléfono): responde `received`, no crea otra intención y no deja ninguna fila en `precheckout_submission_conflicts`. Las dos submissions quedan válidas para el consentimiento.

El `id` no puede ser determinista. Con el mismo `id` y otro `created_at`, la admisión inserta un conflicto en `precheckout_submission_conflicts`, y `_portable_consented_intent_reason` (migración `20260930000100`) descarta para siempre toda submission con un conflicto sin resolver. Nada resuelve esos conflictos, y de esa función dependen el permiso del pago fallido y la audiencia del piloto (migración `20260930000300`): un reintento de GHL dejaría a esa persona sin contacto.

El costo es que la base no distingue un reintento de un segundo envío del mismo contacto: los dos suman una submission a la intención.

## Logs

Una línea por pedido: resultado, motivo, id del formulario, landing, oferta, `delivery_id`, región del teléfono (también en `ghl_phone_unusable`) y si hubo UTM o fbclid. Un envío que no queda admitido (toda respuesta distinta de `200`, salvo el adaptador apagado) sale como warning: el bridge no configura logging, y bajo uvicorn solo los warnings llegan a la salida del contenedor. La admisión sale como info. Nunca el nombre, el email, el teléfono, la IP, el `userAgent`, `contact_id`, el `fbclid`, `fbEventId`, la query, el token ni el cuerpo.

## Riesgos

- **El token es la única barrera.** Con el token, cualquiera admite intenciones con `whatsapp_contact = true` para cualquier teléfono. Mientras esas intenciones no disparen mensajes, el daño se limita a filas en la base. Por eso es **condición para prender** `[flujos].precheckout` (el primer contacto del formulario), o una audiencia `consented_intent` alimentada por este adaptador, una de estas dos cosas:
  - una verificación fuera de banda de cada envío: leer el contacto por la API de GHL con `contact_id` y comparar teléfono y email;
  - la aceptación explícita del riesgo por el responsable de la instancia, por escrito.

  Ninguna de las dos es parte de este contrato v1. La parte del manifiesto la hace cumplir el arranque: con el adaptador prendido, el bridge no arranca si `[flujos].precheckout` está en `true`, y `validate` lo avisa. Levantar esa guarda es el cambio de código que trae la verificación, o el que cita la aceptación escrita. La audiencia `consented_intent` vive en el scope del piloto en la base, fuera de lo que el bridge ve al arrancar: esa parte de la condición no la hace cumplir ningún código.
- **El móvil mexicano normalizado no cruza con las fuentes que no normalizan.** La intención de un `+521…` queda con `normalized_phone = 52…`, sin el `1`. Hotmart guarda los dígitos tal cual (`hotmart.normalize_phone`), y el número de WhatsApp de ATT1 figura en Chatwoot con el `1` (`+5217296521530`, medido el 2026-09-28, cabecera del manifiesto de la instancia). El permiso del pago fallido y la audiencia comparan el teléfono exacto (`_portable_consented_intent_reason`, migración `20260930000100`): si Hotmart o el contacto de WhatsApp traen el mismo móvil con el `1`, la intención admitida por el adaptador da `consented_intent_phone_mismatch` y ese lead se queda sin el contacto, sin aviso. No es peor que sin normalizar (el lead se perdía entero con `422`), pero la normalización no es neutra. Se mide en el E2E de F4.
- **El contacto que vuelve.** La admisión reusa la intención viva sin actualizar `purchase_intents.submitted_at`, y la correlación del pago filtra por esa fecha (migración `20260820000100`). Un contacto que vuelve a enviar el formulario después de `max_lookback`, con una intención viva, no correlaciona. Es del producto y le pasa igual a una landing directa; el adaptador no lo empeora ni lo arregla.

## Prueba de conformidad

Es la condición para prenderlo.

- **Fixtures.** Cada envío real capturado, anonimizado y con fecha y origen, es un fixture en `tests/fixtures/ghl/`. Las variantes de prueba se derivan de ellos: cambiar `mediumId`, quitar un campo, mover un objeto de atribución real, repetir la entrega, repetir una clave. Nunca un payload de GHL escrito de cero.
- **Lo que verifica la suite:**
  1. el evento que produce, con el reloj fijo, pasa `parse_lead_precheckout` con el binding de la instancia y, salvo el `id` aleatorio, es igual a su golden (`tests/fixtures/ghl/expected/`);
  2. la landing y la oferta resueltas son las de la URL;
  3. dos traducciones del mismo cuerpo dan `id` distintos y válidos (ULID de 26 caracteres, Crockford);
  4. un formulario fuera de la lista, una landing desconocida, un teléfono inutilizable o una clave repetida dan `4xx` sin tocar la base;
  5. contra la RPC real (PGlite, `tests/sql/followup_engine/validate_ghl_precheckout_adapter.mjs`): dos entregas del mismo golden producen dos submissions de la misma intención, cero filas en `precheckout_submission_conflicts`, y el envío sigue elegible para el consentimiento (`consented_intent_ok`). Un caso de control con el mismo `id` y otro `created_at` sí deja el conflicto, y documenta por qué el `id` no puede ser determinista.
- **Envío nuevo.** Uno que la suite no acepte se agrega como fixture antes de cambiar el adaptador.

## Lo que este contrato no sabe todavía

- Si todo envío real trae `attributionSource` de primer nivel: hay uno medido.
- Si la acción *Webhook* estándar acepta headers.
- Cuándo reintenta GHL y ante qué códigos.
- Con qué forma guarda GHL los móviles de México (`+52` o `+521`): la única captura mexicana viene sin el `1`. Tampoco cómo llega ese mismo móvil en el pago fallido de Hotmart y en el contacto de WhatsApp (ver [Riesgos](#riesgos)).
- Qué hace el formulario después del envío: si abre el checkout.
- Si el formulario `Om5FpIg5Sr5ce7nSkuPy` (landing `-d`) muestra la aclaración de `att1-whatsapp-contact-v1`.
